#!/bin/bash -l

#SBATCH -N 1
#SBATCH -t 24:00:00
#SBATCH -p regular
#SBATCH -L SCRATCH
#SBATCH --job-name=dump_images
#SBATCH --output=dump_images-%j.out
#SBATCH --error=dump_images-%j.error
#SBATCH -C haswell

python dump_images.py
