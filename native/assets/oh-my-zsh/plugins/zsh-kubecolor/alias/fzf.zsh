# fzf
alias kgprn='kubectl get pods --no-headers --field-selector=status.phase=Running --output=custom-columns=NAME:.metadata.name'
# kubectl options precede --; the command and its arguments follow it.
# With no command, open bash in the selected running pod.
kexec() {
  setopt localoptions pipefail
  local pod
  local -a kubectl_args command_args
  while (( $# )) && [[ $1 != -- ]]; do
    kubectl_args+=("$1")
    shift
  done
  (( $# )) && shift
  command_args=("$@")
  (( ${#command_args} )) || command_args=(bash)
  pod=$(kubectl get pods "${kubectl_args[@]}" --no-headers \
    --field-selector=status.phase=Running \
    --output=custom-columns=NAME:.metadata.name | fzf --prompt=exec-pod) || return
  [[ -n $pod ]] || return 1
  kubectl exec -ti "${kubectl_args[@]}" "$pod" -- "${command_args[@]}"
}
alias klp='kubectl logs "$(kgprn | fzf --prompt=describe-pod:)"'
alias kgpvcn='kubectl get pvc --no-headers --output=custom-columns=NAME:.metadata.name'
alias kgnsn='kubectl get ns --no-headers --output=custom-columns=NAME:.metadata.name'
