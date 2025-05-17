set -ex


md=vit
bs=32
lr=0.05

python main.py eefl         $3  --ft $2 --suffix $1/${2}_${3}/noniid1000 --device $4 --dataset cifar100_noniid1000 --model $md  --lr $lr --bs $bs --rnd 10
