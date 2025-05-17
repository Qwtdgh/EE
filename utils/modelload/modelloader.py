import copy
import json
import importlib

from transformers import AutoTokenizer
from utils.modelload.model import *
from utils.train_utils import *
from peft import PeftModel, PeftConfig, get_peft_model, LoraConfig, TaskType

MNIST = 'mnist'
CIFAR10 = 'cifar10'
CIFAR100 = 'cifar100'
IMAGENET = 'imagenet'
SPEECHCMDS = 'speechcmds'
SVHN = 'svhn'
GLUE = ['sst2', 'mrpc', 'qnli', 'qqp', 'rte', 'wnli']

MLP = 'mlp'
CNN = 'cnn'
VIT = 'vit'
BERT = 'bert'

def check_class_num(dataset):
    return 100 if CIFAR100 in dataset \
        else 10 if CIFAR10 in dataset \
        else 10 if MNIST in dataset \
        else -1

def load_model(args, model_depth=None, exits=None):
    scales = {3:0.4, 6:0.55, 9:0.75, 12:1}
    model_arg = args.model
    dataset_arg = args.dataset
    class_num = check_class_num(dataset_arg)

    if MNIST in dataset_arg:
        if MLP in model_arg:
            model = MLP(args=args, dim_in=784, dim_hidden=256, dim_out=class_num)
        elif CNN in model_arg:
            model = CNNMnist(args=args, dim_out=class_num)
    if CIFAR10 in dataset_arg:
        if CNN in model_arg:
            model = CNNCifar(args=args, dim_out=class_num)
    if CIFAR100 in dataset_arg or SVHN in dataset_arg or dataset_arg in GLUE or IMAGENET in dataset_arg or SPEECHCMDS in dataset_arg:
        
        based_model = importlib.import_module(f'utils.modelload.{model_arg}')
        
        config_path = args.config_path
        pre_model = based_model.Model.from_pretrained(pretrained_model_name_or_path=config_path)
        eq_config = copy.deepcopy(pre_model.config)
        
        num_labels = 100 if CIFAR100 in dataset_arg else 10 if SVHN in dataset_arg else 200 if IMAGENET in dataset_arg else 35 if SPEECHCMDS in dataset_arg else 2
        

        eq_config.num_hidden_layers = model_depth
        
        eq_exit_config = based_model.ExitConfig(eq_config, num_labels=num_labels, exits=exits, policy=args.policy, alg=args.alg) 
        model = based_model.ExitModel(eq_exit_config)
        model.load_state_dict(pre_model.state_dict(), strict=False)

        # tensors = model.parameters_to_tensor((2,5,8,11), is_split=True)
        # print(len(tensors))
        # for tensor in tensors:
        #     print(tensor.shape)
        
        # print(model.parameters_to_tensor().shape)
        # for name, param in model.named_parameters():
        #     print(name, param.shape)
        
        # exit(0)
        
        if dataset_arg in GLUE:
            tokenizer = AutoTokenizer.from_pretrained(
                config_path,
                padding_side="right",
                model_max_length=128,
                use_fast=False,
            )
            model.resize_token_embeddings(len(tokenizer))
    else:
        exit('Error: unrecognized model')
        
    if args.load_path != '':
        existing_model = torch.load(args.load_path)
        model.load_state_dict(existing_model, strict=False)
        
    if args.ft == 'classifier':
        for n, p in model.named_parameters():
            if 'classifier' not in n:
                p.requires_grad = False
    elif args.ft == 'lora':
        peft_config = LoraConfig(task_type=TaskType.SEQ_CLS, r=32, lora_alpha=64, lora_dropout=0.01, target_modules=['query', 'value'])
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()
        for n, p in model.named_parameters():
            if args.policy == 'l2w' or args.dataset == 'sst2':
                if 'accumulator' in n:
                    p.requires_grad = True
            else:
                if 'accumulator' in n or 'classifier' in n:
                    p.requires_grad = True
                            
    # for n, p in model.named_parameters():
    #     print(n, p.requires_grad)
    # exit(0)
    return model

def load_model_eval(args, model_path, config_path=None):
    model_arg = args.model
    dataset_arg = args.dataset
    if CIFAR100 in dataset_arg or SVHN in dataset_arg or dataset_arg in GLUE or SPEECHCMDS in dataset_arg:
        based_model = importlib.import_module(f'utils.modelload.{model_arg}')
           
        exit_config = based_model.Config.from_pretrained(pretrained_model_name_or_path=config_path)
        if args.ft == 'full':
            model = based_model.ExitModel(config=exit_config)
            state_dict = torch.load(model_path)
            model.load_state_dict(state_dict)
            
        elif args.ft == 'lora':
            model = based_model.ExitModel(config=exit_config)
            pre_model = based_model.Model.from_pretrained(pretrained_model_name_or_path=args.config_path)
            model.load_state_dict(pre_model.state_dict(), strict=False)
            peft_config = LoraConfig(task_type=TaskType.SEQ_CLS, r=32, lora_alpha=64, lora_dropout=0.01, target_modules=['query', 'value'])
            model = get_peft_model(model, peft_config)
            state_dict = torch.load(model_path)
            new_dict = {}
            for n, p in state_dict.items():
                new_dict['base_model.model.'+n]=p
            # for n, p in new_dict.items():
            #     print(n)
            # for n, p in model.named_parameters():
            #     if p.requires_grad:
            #         print(n)
            model.load_state_dict(new_dict, strict=False)
        print('model load compeleted')   
    
    else:
        exit('Error: unrecognized model')
    
    return model
