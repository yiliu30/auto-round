SAVE_CHECKPOINT=1 QUANT_INFO_DIR=/home/st_liu/workspace/projects/inc/3rd-party/auto-round/examples/language-modeling/quantized_tmp USE_FLEXROUND=1 python3 main.py --model_name /home/st_liu/workspace/projects/inc/3rd-party/auto-round/examples/language-modeling/quantized_tmp --bits 4 --group_size -1 --iters 20 --enable_minmax_tuning --n_samples 32 --lr 1e-4 --disable_amp