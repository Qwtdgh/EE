import torch.cuda

import torch
from torch import nn
import torch.nn.functional as F
from typing import *

import torch
import torch.utils.checkpoint
from torch import nn

from transformers.modeling_outputs import (
    BaseModelOutputWithPooling,
    ImageClassifierOutput,
)

from transformers.models.vit import ViTPreTrainedModel
from transformers.models.vit.modeling_vit import *
from transformers.models.vit.modeling_vit import ViTForImageClassification as Model
from transformers.models.vit.modeling_vit import ViTConfig as Config

from transformers.models.bert.modeling_bert import *
from utils.modelload.model import BaseModule, Ree


class GradientRescaleFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, weight):
        ctx.save_for_backward(input)
        ctx.gd_scale_weight = weight
        output = input
        return output
    
    @staticmethod
    def backward(ctx, grad_output):
        input = ctx.saved_tensors
        grad_input = grad_weight = None
        if ctx.needs_input_grad[0]:
            grad_input = ctx.gd_scale_weight * grad_output
        return grad_input, grad_weight


gradient_rescale = GradientRescaleFunction.apply


class ExitConfig(ViTConfig):
    
    model_type = "vit"

    def __init__(
        self,
        config, 
        exits=None,
        num_labels=None,
        base_model=None,
        alg=None,
        policy=None,
        **kwargs,
    ):
        super().__init__(
            hidden_size=config.hidden_size,
            num_hidden_layers=config.num_hidden_layers,
            num_attention_heads=config.num_attention_heads,
            intermediate_size=config.intermediate_size,
            hidden_act=config.hidden_act,
            hidden_dropout_prob=config.hidden_dropout_prob,
            attention_probs_dropout_prob=config.attention_probs_dropout_prob,
            initializer_range=config.initializer_range,
            layer_norm_eps=config.layer_norm_eps,
            image_size=config.image_size,
            patch_size=config.patch_size,
            num_channels=config.num_channels,
            qkv_bias=config.qkv_bias,
            encoder_stride=config.encoder_stride,
            **kwargs
        )
        self.exits = exits if exits is not None else tuple(range(config.num_hidden_layers))
        self.classifier_dropout = 0.0
        self.num_labels = num_labels
        self.base_model = base_model
        self.alg = alg
        self.policy = policy


class ViTExitLayer(nn.Module):

    def __init__(self, config: ExitConfig, index: int) -> None:
        super().__init__()
        self.config = config
        self.layer_index = index
        self.exit = True if index in config.exits else False
        if self.exit:
            self.classifier = nn.Linear(config.hidden_size, config.num_labels)
            self.classifier_layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.seq_len_dim = 1
        self.attention = ViTAttention(config)
        self.intermediate = ViTIntermediate(config)
        self.output = ViTOutput(config)
        self.layernorm_before = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.layernorm_after = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        

    def forward(
        self,
        hidden_states: torch.Tensor,
        head_mask: Optional[torch.Tensor] = None,
    ) -> Union[Tuple[torch.Tensor, torch.Tensor], Tuple[torch.Tensor]]:
        self_attention_outputs = self.attention(
            self.layernorm_before(hidden_states),  # in ViT, layernorm is applied before self-attention
            head_mask,
        )
        attention_output = self_attention_outputs[0]
        outputs = self_attention_outputs[1:]  # add self attentions if we output attention weights

        # first residual connection
        hidden_states = attention_output + hidden_states

        # in ViT, layernorm is also applied after self-attention
        layer_output = self.layernorm_after(hidden_states)
        layer_output = self.intermediate(layer_output)

        # second residual connection is done here
        layer_output = self.output(layer_output, hidden_states)
        
        # exit
        if self.exit is True:
            exit_idx = self.config.exits.index(self.layer_index)
            if self.config.policy == 'base' or self.config.policy == 'l2w':
                layer_output_cls = layer_output
                logits = self.classifier(self.classifier_layernorm(layer_output_cls)[:, 0, :])
            elif self.config.policy == 'boosted':
                layer_output_cls = gradient_rescale(layer_output, 1.0/(len(self.config.exits) - exit_idx))
                in_cls = self.classifier_layernorm(layer_output_cls)[:, 0, :]
                logits = self.classifier(in_cls)
                layer_output = gradient_rescale(layer_output_cls, len(self.config.exits) - exit_idx - 1)
                
            outputs = (layer_output_cls, logits, outputs)   
        else:
            outputs = (layer_output, None, outputs)
        
        return outputs


class ViTExitEncoder(nn.Module):
    
    def __init__(self, config: ExitConfig) -> None:
        super().__init__()
        self.config = config
        self.layer = nn.ModuleList([ViTExitLayer(config, index) for index in range(config.num_hidden_layers)])
        self.gradient_checkpointing = False

    def forward(
        self,
        hidden_states: torch.Tensor,
        head_mask: Optional[torch.Tensor] = None,
        stop_exit: Optional[int] = None,
        frozen: Optional[bool] = False,
    ) -> Union[tuple, BaseModelOutput, torch.Tensor]:
        
        exits_logits = ()
        exits_feature = ()
        for i, layer_module in enumerate(self.layer):
            layer_head_mask = head_mask[i] if head_mask is not None else None
            layer_outputs = layer_module(hidden_states, layer_head_mask)
            hidden_states, exit_logits = layer_outputs[0], layer_outputs[1]
            if layer_module.exit:
                exits_logits += (exit_logits,)
                exits_feature += (hidden_states[:, 0, :], )
                hidden_states = hidden_states.detach() if frozen is True else hidden_states
            if stop_exit is not None and i == self.config.exits[stop_exit]: break
        return exits_logits, exits_feature
        


class ViTExitEncoderRee(nn.Module):
    def __init__(self, config: ExitConfig) -> None:
        super().__init__()
        self.config = config
        # TODO more accurate ree config
        base_model = 'small' if self.config.hidden_size == 384 else "tiny"
        self.accumulator = Ree(
            recurrent_steps=1,
            heads=8,
            modulation=True,
            exit_head='normlinear',
            mode='add',
            base_model=base_model,
            num_classes=config.num_labels,
            adapter=None,
            depth=1,
            attn_dim=16,
            mlp_ratio=1.35,

        )
        self.layer = nn.ModuleList([ViTLayer(config) for _ in range(config.num_hidden_layers)])
        self.gradient_checkpointing = False


    def forward(
        self,
        hidden_states: torch.Tensor,
        head_mask: Optional[torch.Tensor] = None,
        output_attentions: bool = False,
        stop_exit: Optional[int] = None,
        frozen: Optional[bool] = False,
    ):
        
        cls_tokens = []
        exits_logits = ()
        exits_feature = ()

        for i, layer_module in enumerate(self.layer):    

            layer_head_mask = head_mask[i] if head_mask is not None else None
            
            layer_outputs = layer_module(hidden_states, layer_head_mask, output_attentions)
            hidden_states = layer_outputs[0]
            cls_token_batch = hidden_states[:, 0][:, None, :]
            cls_tokens.append(cls_token_batch)
            mod_tokens = None
            
            if i in self.config.exits:
                # TODO hidden_states需要修改，添加最开始的cls token
                # 取出第位于0位置的 cls_tokens (batch*1*hidden_size)

                # exit_cls_tokens是在1维上的cat (batch*exit_idx*hidden_size)
                exit_cls_tokens = torch.cat((cls_tokens), 1)
                
                # print(f'exit_cls_tokens: {exit_cls_tokens.shape}')
                mod_tokens = self.accumulator(exit_cls_tokens)
                _outputs = self.accumulator.head(mod_tokens[:, 0] + exit_cls_tokens[:, -1])
                # 记录每个exit的logits  exits_num * (batch*label_nums)
                exits_logits += (_outputs, )
                exits_feature += (hidden_states[:, 0], )
            
            if self.accumulator.modulation:
                if mod_tokens is None:
                    mod_tokens = self.accumulator(torch.cat((cls_tokens), 1))
                    hidden_states[:, 0] = mod_tokens[:, -1]
                        
            
            if stop_exit is not None and i == self.config.exits[stop_exit]: break


        return exits_logits, exits_feature


class ViTExitModel(ViTPreTrainedModel):
    
    def __init__(self, config: ExitConfig, add_pooling_layer: bool = True, use_mask_token: bool = False):
        super().__init__(config)
        self.config = config

        self.embeddings = ViTEmbeddings(config, use_mask_token=use_mask_token)
        
        self.encoder = ViTExitEncoder(config) if self.config.alg != 'reefl' else ViTExitEncoderRee(config)
        # self.encoder = ViTExitEncoder(config)
        # self.layernorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.pooler = ViTPooler(config) if add_pooling_layer else None

        self.post_init()

    def forward(
        self,
        pixel_values: Optional[torch.Tensor] = None,
        bool_masked_pos: Optional[torch.BoolTensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        interpolate_pos_encoding: Optional[bool] = None,
        is_latent: Optional[bool] = False,
        stop_exit:Optional[int] = None,
        frozen:Optional[bool] = None,
        rt_embedding:Optional[bool] = False
    ) -> Union[Tuple, BaseModelOutputWithPooling, torch.Tensor]:
        if is_latent is False:
            
            head_mask = self.get_head_mask(head_mask, self.config.num_hidden_layers)

            expected_dtype = self.embeddings.patch_embeddings.projection.weight.dtype
            if pixel_values.dtype != expected_dtype:
                pixel_values = pixel_values.to(expected_dtype)
            embedding_output = self.embeddings(
                pixel_values, bool_masked_pos=bool_masked_pos, interpolate_pos_encoding=interpolate_pos_encoding
            )

            hidden_states = embedding_output
        else:
            hidden_states = pixel_values
        
        
        if rt_embedding:
            return hidden_states
        
        exits_logits, exits_feature = self.encoder(
            hidden_states,
            head_mask=head_mask,
            stop_exit=stop_exit,
            frozen=frozen
        )
            
        return (exits_logits, exits_feature)



class ExitModel(ViTPreTrainedModel, BaseModule):
    
    def __init__(self, config: ExitConfig) -> None:
        ViTPreTrainedModel.__init__(self, config)
        BaseModule.__init__(self)

        self.num_labels = config.num_labels
        # if config.classifier_archi == 'ree':
        #     self.vit = ViTModelRee(config, add_pooling_layer=False)
        # else:
        #     self.vit = ViTModel(config, add_pooling_layer=False)
        #     for i in config.exits:
        #         setattr(self, f"classifier_{i}", nn.Linear(config.hidden_size, config.num_labels))
        self.vit = ViTExitModel(config, add_pooling_layer=False)
        self.post_init()

    def forward(
        self,
        pixel_values: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        interpolate_pos_encoding: Optional[bool] = None,
        is_latent: Optional[bool] = False,
        stop_exit:Optional[int] = None,
        rt_embedding:Optional[bool]=False,
        rt_feature:Optional[bool]=False,
        frozen:Optional[bool]=False,
        input_ids=None, 
        attention_mask=None,
        inputs_embeds=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ) -> Union[tuple, ImageClassifierOutput, torch.Tensor]:
        
        outputs = self.vit(
            pixel_values,
            head_mask=head_mask,
            interpolate_pos_encoding=interpolate_pos_encoding,
            stop_exit=stop_exit,
            is_latent=is_latent,
            rt_embedding=rt_embedding,
            frozen=frozen
        )
        if rt_embedding: return outputs
        if rt_feature: return outputs
        return outputs[0]

        # sequence_output = outputs[0]
        # logits = self.classifier(sequence_output[:, 0, :])
        
        # if self.config.classifier_archi == 'ree':
        #     all_logits = outputs
        #     logits = outputs[self.config.exits[-1]]
        # else:
        #     hidden_states = BaseModelOutputWithPooling(outputs).hidden_states
        #     logits = getattr(self, f"classifier_{self.config.exits[-1]}")(self.layernorm(hidden_states[self.config.exits[-1]])[:, 0, :])
            
        #     all_logits = tuple()
        #     for i in range(len(self.config.exits)):
        #         exit = self.config.exits[i]
        #         classifier = getattr(self, f"classifier_{exit}")
        #         all_logits += (classifier(self.layernorm(hidden_states[exit + 1])[:, 0, :]), )
        