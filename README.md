# hpc_scripts
Scripts for easier use of the QUT HPC system. These are available once you run the following (soon this will be done automatically)
```
source /work/microbiome/sw/hpc_scripts/cmr_bashrc_extras.bash
```

# mqsub
mqsub aims to be an ergonomic method for submitting jobs to the PBS queue. It dynamically generates a qsub script, submits it, 
waits for the result and then emails you to say it is done.

A straightforward example:

```
$ mqsub -- echo hello world
12/23/2020 10:38:30 AM INFO: qsub stdout was: 8690551.pbs
12/23/2020 10:38:30 AM INFO: First status of job is: Q: Job is queued
12/23/2020 10:41:31 AM INFO: Now status of job is: F: Job is finished
12/23/2020 10:41:31 AM INFO: Job has finished
12/23/2020 10:41:31 AM INFO: resources_used.walltime: 00:00:01
12/23/2020 10:41:31 AM INFO: resources_used.cpupercent: 0
12/23/2020 10:41:31 AM INFO: resources_used.cput: 00:00:00
12/23/2020 10:41:31 AM INFO: resources_used.vmem: 67728kb
hello world
```

From there the behaviour can be modified in several ways, using the optional arguments. For instance to request 1 hour instead of 1 week:

```
$ mqsub --hours 1 -- echo hi
```

There are several other options, which can be viewed with `mqsub -h`
```mqsub -h
                            _
                           | |
  _ __ ___   __ _ ___ _   _| |__
 | '_ ` _ \ / _` / __| | | | '_ \
 | | | | | | (_| \__ \ |_| | |_) |
 |_| |_| |_|\__, |___/\__,_|_.__/  Centre for Microbiome Research, QUT
               | |
               |_|
Example usage:
    mqsub -t 24 -m 250 --hours 168 -- aviary recover --pe-1 $R1 --pe-2 $R2 --max-threads 24 --n-cores 24 --output $runID.aviary.output
    mqsub -t 8 -m 32 --hours 168 --command-file file.txt --chunk-num 5

positional arguments:
  command               command to be run

optional arguments:
  -h, --help            show this help message and exit
  --debug               output debug information
  --quiet               only output errors
  -t CPUS, --cpus CPUS  Number of CPUs to queue job with [default: 1]
  -g GPU, --gpu GPU     Number of GPUs to use [default: 0]
  -m MEM, --mem MEM, --ram MEM
                        GB of RAM to ask for [default: 8*num_cpus]
  --directive DIRECTIVE
                        Arbitrary PBS directory to add e.g. '-l ngpus=1' to ask for a GPU [default: Not used]
  -q QUEUE, --queue QUEUE
                        Name of queue to send to [default: 'microbiome']
  --hours HOURS         Hours to run for [default: 1 week]
  --days DAYS           Days to run for [default: 1 week]
  --weeks WEEKS         Weeks to run for [default 1]
  --name NAME           Name of the job [default: first word of command]
  --dry-run             Print script to STDOUT and do not lodge it with qsub
  --bg                  Submit the job, then quit [default: wait until job is finished before exiting]
  --no-email            Do not send any emails, either on job finishing or aborting
  --script SCRIPT       Script to run, or "-" for STDIN
  --script-shell SCRIPT_SHELL
                        Run script specified in --script with this shell [default: /bin/bash]
  --script-tmpdir SCRIPT_TMPDIR
                        When '--script -' is specified, write the script to this location as a temporary file
  --poll-interval POLL_INTERVAL
                        Poll the PBS server once every this many seconds [default: 30]
  --no-executable-check
                        Usually mqsub checks the executable is currently available. Don't do this [default: do check]
  --command-file COMMAND_FILE
                        A file with list of newline separated commands to be split into chunks and submitted. One command per line. mqsub --command-file <file.txt> --chunk-num <int>
  --chunk-num CHUNK_NUM
                        Number of chunks to divide the commands (from --command-file) into
  --chunk-size CHUNK_SIZE
                        Number of commands (from --command-file) per a chunk
  --prelude PRELUDE     Code from this file will be run before each chunk
  --scratch-data SCRATCH_DATA [SCRATCH_DATA ...]
                        Data to be copied to a scratch space prior to running the main command(s). Useful for databases used in large chunks of jobs.
  --run-tmp-dir         Executes your command(s) on the local SSD ($TMPDIR/mqsub_processing) of a node. IMPORTANT: Use absolute paths for your input files, and a relative path for your output.
  --depend DEPEND [DEPEND ...]
                        Space separated list of ids for jobs this job should depend on.
  --segregated-log-files
                        Put log files in ~/qsub_logs/<date>/<directory> instead of the current working directory.

----------------------------------------------------------------------------------------------------------
Full README can be found on the CMR github - https://github.com/centre-for-microbiome-research/hpc_scripts
Further information can also be found in the CMR Compute Notes -  https://tinyurl.com/cmr-internal-compute
```

If you have a large amount of commands (one command per line in a single file) you wish to split in a number of qsub jobs, you can do the following:
```
mqsub --command-file <file> --chunk-num <int>
```
or
```
mqsub --command-file <file> --chunk-size <int>
```
As per regular mqsub you should specify the amount of resource for the qsub scripts, i.e:
```
mqsub -t 16 -m 32 --hours 24 --command-file <file> --chunk-num <int>
```

You can also speed up some of your processes by copying data files to a node's SSD prior to running commands. One good use would to copy a database to the SSD and then run a chunk of commands (as per above). To do this, use the `--scratch-data` option for which multiple paths can be specified. In your mqsub some command you'll need to adjust how you specify the location of the copied files (which are copied to $TMPDIR), for example:
```
mqsub --scratch-data ~/gtdb_r207.reassigned.v5.sdb ~/S3.metapackage_20220513.smpkg -- singlem --db1 \$TMPDIR/gtdb_r207.reassigned.v5.sdb \$TMPDIR/S3.metapackage_20220513.smpkg
```

Certain workflows demand a lot of IO (for example: assemblies, or any multiple instances of a program run in parrallel). This can put strain upon the filesystem and it's reccomended to run these workflows on the 1TB SSD of each node. To do this, specify `--run-tmp-dir`, and your the files in your output directory will be processed on the SSD and then copied back to your current working directory once complete. You can use this functionality for regular mqsub, as well as chunks (the output will be moved back to Lustre once all jobs in the chunk complete).
**IMPORTANT:** you must specify absolute paths to your input files and a relative path for your output directory. For example:
```
mqsub \
-t 32 \
-m 250 \
-- aviary complete \
--pe-1 /work/microbiome/human/fastq/reads_R1.fastq.gz \
--pe-2 /work/microbiome/human/fastq/reads_R2.fastq.gz \
--output aviary_output1 \
--run-tmp-dir

```


# mqdel
To delete PBS jobs for the current user, use `mqdel`. It queries `qstat` and calls `qdel` on matching jobs.

Delete all queued jobs:
```
mqdel --queued
```

Delete all batch jobs:
```
mqdel --all
```

Preview which jobs would be deleted without actually deleting them:
```
mqdel --queued --dry-run
mqdel --all --dry-run
```

# mqstat
To view useful usage statistics (i.e. the percentage of microbiome queue CPUs which are currently in-use/available) simply type `mqstat`. Example output:
```
Microbiome group jobs running: 4 / 4 (100.00%)
Microbiome group CPUs utilized: 120 / 512 (23.44%)
Microbiome group CPUs queued: 0
sternesp jobs running: 0 / 0 (0.0%)
sternesp CPUs running: 0 / 512 (0.0%)
sternesp CPUs queued: 0
Non-microbiome group jobs / CPU: 0 / 0 (0.0%)
```

For an interactive view of job status, use `mqtop`. Navigate with the arrow
keys or `j`/`k`, or select rows with the mouse. Use `--max-jobs` to limit the
number of jobs retrieved (default 500). Press `/` to search by job name or ID.
`l` or `o` shows a job's stdout log (using `ssh-to-job` for running jobs), `e`
shows stderr, `f` runs `qstat -xf` for full details, `s` opens an interactive
shell on a running job, `r` refreshes the display, and `u` hides or shows queued
jobs. A help footer summarises these keys. This command replaces the old
`mqstat --watch` option.

You can also view a detailed breakdown queued and running jobs on a per-user basis by typing `mqstat --list`. Example output:
```
List of jobs in queue:
----------------------
PBS ID      | username  | name        | state | CPUs | RAM   | walltime (hrs) | start time               | elapsed time         | % walltime | microbiome queue | node       
----------- | --------- | ----------- | ----- | ---- | ----- | -------------- | ------------------------ | -------------------- | ---------- | ---------------- | -----------
9524869.pbs | n10853499 | Rhys Newell | R     | 48   | 480gb | 84             | Tue Jun  8 03:02:37 2021 | 2 day, 6 hr, 19 min  | 64         | yes              | cl5n013/0*0
9524870.pbs | n10853499 | Rhys Newell | R     | 48   | 480gb | 84             | Tue Jun  8 20:57:54 2021 | 1 day, 12 hr, 24 min | 43         | yes              | cl5n012/0*0

Time left until maintenance (hr:min:sec): 1999:37:25
```

Furthermore, you can also view the amount of available resources per-node basis by typing `mqstat --avail`. This can help you plan on how many resources to request. In the example below, if you were to request 49+ CPUs your job will be held in the queue until that amount of resource is available on a single node.
```
Available resources:
--------------------
node    | CPU        | RAM   
------- | ---------- | ------
cl5n010 | 0 threads  | 560 GB
cl5n011 | 16 threads | 550 GB
cl5n012 | 48 threads | 480 GB
cl5n013 | 1 threads  | 356 GB
```


# mqwait
If you are running a batch of jobs, you may wish to be notified once the entire batch has finished processing, as opposed to per-job notifications generated by mqsub. There are three ways to run mqwait:

1) Typing `mqwait` will send you an email once all your current running and queued jobs finish.

2) You can specify a newline delimited list of PBS jobs you wish to be notified by. ie. `mqwait -i pbs_job_list`
In this example pbs_job_list would contain:
```
123456.pbs
123457.pbs
123458.pbs
```

3) You can also pipe multiple mqsubs (using parallel or a script containing mqsub commands) in a single command to be notified when that specific batch of jobs finish. Note: currently the STDERR from mqsub is piped into mqwait using |&. This may change in future.
`parallel mqsub --no-email --bg ... |& mqwait -m` or `mqsub_jobs.sh |& mqwait -m`

Specifying the `-l` parameter will verbosely display the number of remaining jobs on your terminal and it controlled by the polling rate `-p` (default 60 seconds)


# mcreate
This is a basic script which searches for the latest version of conda package and creates a new, versioned, environment using conda (with the `-c` parameter) or mamba (default; requires mamba to be installed first).

Simply: `mcreate <package_name>`

You can also check for the latest version of a package without installing:
`mcreate <package_name> -v`


# pixi_cmr_init
This script initializes a pixi project with CMR-specific settings. It creates a `pixi.toml` file using `pixi init` and modifies the channels to include `bioconda` after `conda-forge`. It also creates a `.pixi` directory in a centralized location and symlinks it to the project directory.

Basic usage:
```bash
pixi_cmr_init.py                    # Initialize in current directory
pixi_cmr_init.py /path/to/project   # Initialize in specific directory
pixi_cmr_init.py --dry-run          # Show what would be done without executing
```

Features:
- Automatically adds `bioconda` channel after `conda-forge` in the channels list
- Creates `.pixi` directory in `/pkg/cmr/<username>/pixi_dirs/<path>.pixi` for centralized storage
- Creates a symlink from project directory to the centralized `.pixi` directory
- Falls back to local `.pixi` directory if centralized location is not accessible
- Warns if `.pixi` directory or symlink already exists
- Supports dry-run mode to preview changes
- Works with or without the `toml` Python package (uses fallback parsing if needed)

Directory Structure:
- Actual `.pixi` directory: `/pkg/cmr/<username>/pixi_dirs/<project_path>.pixi`
- Symlink in project: `<project_directory>/.pixi -> /pkg/cmr/<username>/pixi_dirs/<project_path>.pixi`
- Path conversion: `/home/user/project` becomes `home_user_project.pixi`

Example output:
```bash
$ pixi_cmr_init.py /home/user/my_project
Initializing pixi project in: /home/user/my_project
Running 'pixi init'...
Modified /home/user/my_project/pixi.toml to include bioconda channel
Channels: ['conda-forge', 'bioconda']
Created .pixi directory at /pkg/cmr/user/pixi_dirs/home_user_my_project.pixi
Created symlink: /home/user/my_project/.pixi -> /pkg/cmr/user/pixi_dirs/home_user_my_project.pixi
pixi_cmr_init completed successfully!
```

Fallback behavior:
If `/pkg/cmr/<username>/pixi_dirs/` is not accessible (e.g., permissions, directory doesn't exist), the script automatically falls back to creating a local `.pixi` directory in the project folder.


# CMR Compute Notes
Further information can also be found in the CMR Compute Notes -  https://tinyurl.com/cmr-internal-compute
