# Small test workflow to confirm the mqyolo sandbox can submit per-rule PBS
# jobs via `snakemake --profile aqua`. Each rule becomes its own queue job
# (mqsub -> qsub on the host), and rule `b` depends on `a` so job
# dependencies are exercised too.
#
#   snakemake --profile aqua --snakefile sandbox_test.smk
#
rule all:
    input:
        "sandbox_test_b.txt",


rule a:
    output:
        "sandbox_test_a.txt",
    threads: 1
    resources:
        mem_mb=1000,
        runtime="10m",
    log:
        "logs/sandbox_test_a.log",
    shell:
        "(echo 'rule a ran on PBS host:' $(hostname); date) > {output} 2> {log}"


rule b:
    input:
        "sandbox_test_a.txt",
    output:
        "sandbox_test_b.txt",
    threads: 1
    resources:
        mem_mb=1000,
        runtime="10m",
    log:
        "logs/sandbox_test_b.log",
    shell:
        "(cat {input}; echo 'rule b ran on PBS host:' $(hostname); date) > {output} 2> {log}"
