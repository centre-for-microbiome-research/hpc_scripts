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
```
optional arguments:
  -h, --help            show this help message and exit
  --debug               output debug information
  --quiet               only output errors
  -t CPUS, --cpus CPUS  Number of CPUs to queue job with [default: 1]
  -m MEM, --mem MEM, --ram MEM
                        GB of RAM to ask for [default: 4*num_cpus]
  --directive DIRECTIVE
                        Arbitrary PBS directory to add e.g. '-l ngpus=1' to
                        ask for a GPU [default: Not used]
  -q QUEUE, --queue QUEUE
                        Name of queue to send to, or '-' to not specify
                        [default: '-']
  --hours HOURS         Hours to run for [default: 1 week]
  --weeks WEEKS         Weeks to run for [default 1]
  --name NAME           Name of the job [default: first word of command]
  --dry-run             Print script to STDOUT and do not lodge it with qsub
  --bg                  Submit the job, then quit [default: wait until job is
                        finished before exiting]
  --no-email            Do not send any emails, either on job finishing or
                        aborting
  --script SCRIPT       Script to run, or "-" for STDIN
  --script-shell SCRIPT_SHELL
                        Run script specified in --script with this shell
                        [default: /bin/bash]
  --script-tmpdir SCRIPT_TMPDIR
                        When '--script -' is specified, write the script to
                        this location as a temporary file
  --poll-interval POLL_INTERVAL
                        Poll the PBS server once every this many seconds
                        [default: 30]
  --no-executable-check
                        Usually mqsub checks the executable is currently
                        available. Don't do this [default: do check]
```
