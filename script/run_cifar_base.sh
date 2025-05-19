set -ex


md=vit
bs=32
lr=0.01

python main.py eefl         $2          --ft full --suffix $1/full_${2}/noniid1000     --device $3 --dataset cifar100_noniid1000 --model $md  --lr $lr --bs $bs --rnd 10
python main.py eefl         $2          --ft lora --suffix $1/lora_${2}/noniid1000     --device $3 --dataset cifar100_noniid1000 --model $md  --lr $lr --bs $bs --rnd 10
