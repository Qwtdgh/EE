set -ex

sr=0.1
total_num=100

bs=32
lr=0.05
md=bert
cp=models/google-bert/bert-12-128-uncased
g=0.99
ds=qnli
python main.py eefl             $2    --ft full --suffix $1/full_${2}/${ds} --device $3 --dataset $ds --model $md --sr $sr --total_num $total_num --lr $lr    --bs $bs --config_path $cp
python main.py eefl             $2    --ft lora --suffix $1/lora_${2}/${ds} --device $3 --dataset $ds --model $md --sr $sr --total_num $total_num --lr $lr    --bs $bs --config_path $cp
