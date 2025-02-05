A snakemake workflow for basecalling nanopore reads using dorado.

To use, copy the config.yaml file to your working directory and edit the parameters as needed, then run the following command:

```
snakemake --profile aqua --snakefile /work/microbiome/sw/hpc_scripts/dorado_workflow/Snakefile --configfile config.yaml
```

Improvements for the future:
* Pipe samtools merge -u into dorado demux, to avoid a large intermediate file
* Generate fastq files with samtools fastq from demux output instead
