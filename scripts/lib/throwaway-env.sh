# Sourced by the scripts that start a throwaway stack (M5-13 review). Not run by itself.
#
# Compose reads a variable from the calling shell before the env file. In a shell where a real
# installation's settings are exported (`set -a; . deploy/.env`, a profile), a throwaway stack would
# take over that installation's model key, tokens and identity-provider secret, its bind address and
# profiles, and its image tag (PHAROS_IMAGE_TAG: `up --build` would re-tag the installation's images,
# and the clean-up would remove the wrong ones). The generated env file must be the only source, so
# every PHAROS_*, POSTGRES_* and COMPOSE_* variable is dropped, except the ones the script itself
# reads to choose its ports, named as arguments; read them before calling this.
drop_installation_env() {
  local name keep
  while read -r name; do
    for keep in "$@"; do
      [ "$name" = "$keep" ] && continue 2
    done
    unset "$name"
  done < <(compgen -e | grep -E '^(PHAROS_|POSTGRES_|COMPOSE_)' || true)
}
