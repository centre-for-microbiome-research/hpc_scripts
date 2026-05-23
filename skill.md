# CMR HPC Skills Reference

A practical reference for using QUT's Centre for Microbiome Research (CMR) HPC cluster ("aquarius" / aqua).

---

## Accessing the HPC

### Account Setup
- Complete the HPC account request form through QUT eResearch
- Identify required permission groups via the CMR folders PIs permission sheet
- Submit a ticket to be added to project folders; eResearch will confirm with PI
- All CMR personnel should join the `microbiome` group

### SSH Login
```bash
ssh {username}@aqua.qut.edu.au   # VPN required off-campus
```

**Windows:** Windows Terminal or MobaXTerm  
**Mac/Linux:** native terminal

### Public Key Authentication

macOS/Linux:
```bash
ssh-keygen
ssh-copy-id {username}@aqua.qut.edu.au
```

Windows PowerShell:
```powershell
ssh-keygen
type .ssh\id_rsa.pub | ssh aqua "cat >> .ssh/authorized_keys"
```

Windows SSH agent (run as admin):
```powershell
Set-Service ssh-agent -StartupType Automatic
Start-Service ssh-agent
Get-Service ssh-agent
```

---

## Running Jobs

### 1. Head Node (direct execution)
Simplest approach; limited CPU/RAM/I/O. No time limit. Use `screen` or `tmux` for persistence.

### 2. Interactive Jobs

```bash
mqinteractive             # standard interactive session (12h max)
mqinteractive a           # attach to existing session
mqinteractive gpu         # GPU session
```

Manual CPU (max 8 CPUs / 32 GB):
```bash
qsub -I -S /bin/bash -l select=1:ncpus=8:mem=32GB,walltime=12:00:00
```

Manual CPU+GPU (max 12 CPUs / 64 GB / 2 GPU MIGs):
```bash
qsub -I -S /bin/bash -l select=1:ncpus=6:ngpus=1:mem=34gb -l walltime=12:00:00
```

SSH into a running PBS job:
```bash
/pkg/hpc/scripts/ssh-to-job <JOB_ID>
```

### 3. Batch Jobs via `mqsub`

Batteries-included wrapper around `qsub`. Access to ~10,000 CPUs. 48-hour walltime max.

```bash
mqsub -- echo hello world
mqsub --command-file commands.txt
mqsub --chunk-num 10 -- mycommand          # split into 10 jobs
mqsub --run-tmp-dir -- mycommand           # use local node SSD
mqsub --scratch-data /path/to/db -- cmd    # copy DB to /scratch first
```

Full docs: https://github.com/centre-for-microbiome-research/hpc_scripts/blob/main/README.md#mqsub

**Tips:**
- Overestimate resources slightly to avoid failure
- Submit many small jobs rather than one large job
- Avoid GPUs unless necessary

### 4. Traditional PBS Scripts

Manually write PBS scripts with `#PBS` directives. See QUT eResearch documentation.

### 5. Snakemake Workflows

Test locally:
```bash
snakemake --profile local
```

Submit to cluster:
```bash
snakemake --profile aqua
```

### 6. Jupyter Notebooks

Available on the head node, interactive jobs, or via VSCode tunnels.

---

## Monitoring Utilities

### `mqtop` — interactive job monitor
Arrow keys or mouse to select jobs. Color coding: green (finished), red (failed), grey (running), orange (queued).  
Press `o` for stdout, `s` to SSH into job node, `h` for help. Auto-refreshes every 10 minutes.

### `mqstat` — quick job overview
```bash
mqstat
```
Shows personal jobs, CMR-wide resource usage, and cluster totals.

### `mqwait` — notify on job completion
```bash
mqwait                        # wait for all jobs
mqwait -i pbs_job_list        # wait for specific jobs
```

### Grafana Dashboard
https://hpc-monitoring.eres.qut.edu.au/  
Login: aqua username (without @qut.edu.au). Navigate to **Dashboards → My PBS Jobs Usage**.

### PBS Commands
```bash
qstat -u $(whoami)            # list your jobs
qjobs                         # alias
qdel <ID>.pbs                 # cancel job
qdel -W force <ID>            # force cancel
qselect -u $(whoami) | xargs qdel   # cancel all jobs
qstat -xf <ID>.pbs            # detailed job info
pbsnodeinfo                   # node utilization
```

Job stdout/stderr while running:
```
/var/spool/PBS/spool/<job_id>.pbs.OU   # stdout
/var/spool/PBS/spool/<job_id>.pbs.ER   # stderr
```

---

## Software Management

### Central Conda Environment
Pre-installed, actively maintained. Conda loads automatically at login.

```bash
ls /work/microbiome/sw/conda/envs   # list environments
conda env list
```

### `mcreate` — search and create conda environments
```bash
mcreate <package>       # create environment with latest version
mcreate <package> -v    # check version only
```

### Personal Conda Setup

Configure channels:
```bash
conda config --add channels conda-forge
```

Manual `.condarc`:
```yaml
channels:
  - conda-forge
  - bioconda
  - defaults
envs_dirs:
  - /pkg/cmr/<username>/conda/envs
  - /work/microbiome/sw/conda/envs
pkgs_dirs:
  - /pkg/cmr/<username>/conda/pkgs
```

Create env directory and symlink:
```bash
mkdir -p /pkg/cmr/$(whoami)/conda/envs
ln -s /pkg/cmr/$(whoami)/conda/envs ~/e
```

Create and activate environment:
```bash
mcreate megahit                        # or:
conda create -p /pkg/cmr/$(whoami)/conda/envs/megahit-v1.2.9 megahit=1.2.9
conda activate megahit-v1.2.9
```

Optional alias: `echo "alias a='conda activate'" >> ~/.bashrc`

### Pixi Workspaces

```bash
pixi init {workspace}
pixi add {package}=={version}
pixi install
pixi run {command}
pixi shell; {command}
```

Multi-environment `pixi.toml`:
```toml
[workspace]
channels  = ["conda-forge"]
name      = ["my-workspace"]
platforms = ["linux-64"]

[dependencies]
python    = ">=3.11"
snakemake = ">=8"

[feature.coverm.dependencies]
coverm = "*"

[feature.singlem.dependencies]
singlem = "*"

[environments]
coverm  = ["coverm"]
singlem = ["singlem"]
combo   = ["coverm", "singlem"]

[tasks]
test = { cmd = "pytest", default-environment = "test" }
```

Run with specific environment:
```bash
pixi run -e coverm coverm …
pixi shell -e coverm
```

Snakemake integration:
```python
rule example:
    shell:
        "pixi run -e singlem singlem pipe -1 reads.fa -p out.tsv"
```

### Environment Modules
```bash
module avail
```

### CMR bashrc extras
Auto-sourced from `/work/microbiome/sw/hpc_scripts/cmr_bashrc_extras.bash`.

Disable: `touch ~/.nombenv`  
Manual: `echo 'source /work/microbiome/sw/hpc_scripts/cmr_bashrc_extras.bash' >> ~/.bashrc`

VSCode terminal fix — add to `.bashrc`:
```bash
__conda_setup="$('/work/microbiome/sw/conda/bin/conda' 'shell.bash' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
else
    if [ -f "/work/microbiome/sw/conda/etc/profile.d/conda.sh" ]; then
        . "/work/microbiome/sw/conda/etc/profile.d/conda.sh"
    else
        export PATH="/work/microbiome/sw/conda/bin:$PATH"
    fi
fi
unset __conda_setup
```

---

## Storage

| Path | Description |
|------|-------------|
| `/work/microbiome` | Slow, persistent storage |
| `/work/microbiome/users` | Personal user folders |
| `/work/microbiome/db` | Shared databases and datasets |
| `/work/microbiome/scratch` | WEKA system — fast, all nodes, not backed up |
| `/scratch` | Fast, not for long-term storage |
| `/data1` | Per-node large, very fast temporary storage |

Convenient symlinks:
```bash
ln -s /work/microbiome ~/m
ln -s /work/microbiome/scratch ~/scratch-microbiome
```

### `mpermissions` — apply group permissions
```bash
mpermissions -g <group_name> <folder_name>
```

### `mqdel` — terminate batch jobs
```bash
mqdel --queued              # delete queued jobs only
mqdel --all                 # delete all jobs
mqdel --queued --dry-run    # preview
mqdel --all --dry-run
```

### Mounting Network Drives

**Windows (SSHFS):**
```
net use Z: \\sshfs\<username>@aqua.qut.edu.au/../../work/microbiome/<folder>
```

**Linux (SSHFS):**
```bash
mkdir ~/microbiome
sshfs aqua.qut.edu.au:/work/microbiome ~/microbiome
```

**Linux (SAMBA — 5–10× faster, requires cifs-utils):**
```bash
export HPC_USER=<username>
export PASSWD=<password>
mkdir microbiome
sudo --preserve-env=PASSWD mount -t cifs \
  -o "uid=$UID,gid=$GID,username=${HPC_USER}@qut.edu.au" \
  //hpc-fs.qut.edu.au/work/microbiome ./microbiome
```

---

## Remote Coding (VSCode Tunnels)

**Install VSCode on aqua** (run in interactive job):
```bash
mkdir -p /pkg/cmr/$(whoami)
cd /pkg/cmr/$(whoami)
wget 'https://code.visualstudio.com/sha/download?build=stable&os=linux-x64' -O vscode.tar.gz
tar xf vscode.tar.gz
wget 'https://code.visualstudio.com/sha/download?build=stable&os=cli-alpine-x64' -O vscode-cli.tar.gz
tar xf vscode-cli.tar.gz
/pkg/cmr/$(whoami)/code version use stable --install-dir /pkg/cmr/$(whoami)/VSCode-linux-x64/
ln -s /pkg/cmr/$(whoami)/code ~
```

**Start tunnel:**
```bash
mqinteractive
~/code tunnel --name aqua   # requires GitHub authentication
```

Connect from local VSCode: `Ctrl+Shift+P` → **Remote-Tunnels: Connect to Tunnel…**

**Auto-start tunnel on interactive session** (add to `.bashrc`):
```bash
if [[ ${PBS_ENVIRONMENT} == "PBS_INTERACTIVE" ]]; then
    screen -wipe
    if ! screen -list | grep -q vscode; then
        screen -dmS vscode ~/code tunnel --name aqua
    fi
fi
```

**Troubleshooting:**
- Tunnel invisible: `~/code tunnel --name aqua` to restart
- Python envs not visible in Jupyter: disable Python Environments extension
- Slow indexing: open specific project directories, not home (avoids crawling symlinks)

**Home/End keys fix** (add to `~/.inputrc`, reload with `bind -f ~/.inputrc`):
```
"\e[1~": beginning-of-line
"\e[4~": end-of-line
"\e[H":  beginning-of-line
"\e[F":  end-of-line
"\eOH":  beginning-of-line
"\eOF":  end-of-line
```

---

## Dorado Base Calling (Long Reads)

Snakemake workflow for converting nanopore POD5 → FASTQ on GPU nodes.  
Details: https://github.com/centre-for-microbiome-research/hpc_scripts/tree/main/dorado_workflow

---

## Remote Access

- **Eduroam** with QUT email
- **QUT VPN**: remote.qut.edu.au
- **On-campus** ethernet

---

## Bash History

Permanent history in `~/.bash_eternal_history` — old commands are never forgotten (via CMR bash extras).

---

## Support

| Resource | Link |
|----------|-------|
| eResearch tickets | https://eresearchqut.atlassian.net/servicedesk/customer/portals |
| eResearch docs (VPN) | https://docs.eres.qut.edu.au/ |
| CMR internal compute | https://tinyurl.com/cmr-internal-compute |
| CMR GitHub | https://github.com/centre-for-microbiome-research |
| Grafana monitoring | https://hpc-monitoring.eres.qut.edu.au/ |
