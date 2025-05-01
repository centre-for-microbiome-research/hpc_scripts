# Add HPC scripts e.g. mqsub to the path
export PATH="/mnt/hpccs01/work/microbiome/sw/hpc_scripts/bin:$PATH"

#function for command prompt and email notification of job completion
function notify { command "$@" && success || fail; }

# Save all the history, see https://debian-administration.org/article/543/Bash_eternal_history
export HISTTIMEFORMAT="%F %T "
PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND ; }"'echo $$ $USER \
               "$(history 1)" >> ~/.bash_eternal_history'

# Setup snakemake config directories so it interfaces well with the PBS system
mkdir -p ~/.config/snakemake
cd ~/.config/snakemake && ln -sf /work/microbiome/sw/hpc_scripts/snakemake_configs/* . && cd $OLDPWD

# Setup for mqinteractive. If interactive and latest.sh exists, load 'er up.
# To solve the issue of ssh-to-job to interactive job acting like starting an interactive, we need different logic to test it. Using the $HOSTNAME will work, and we’ll just have to deal with it down the track if we end up getting more interactive hosts
if ([[ ${PBS_ENVIRONMENT} == "PBS_INTERACTIVE" ]] || [[ ${HOSTNAME} == "cpu1n001" ]] || [[ ${HOSTNAME} == "gpu1n001" ]]) && [[ -f $HOME/.hpc_scripts/mqinteractive_scripts/latest.sh ]]; then
    source $HOME/.hpc_scripts/mqinteractive_scripts/latest.sh
fi

# Add mqinteractive as an alias, so that the history is saved before running mqinteractive
alias mqinteractive='history -a; real-mqinteractive'
