#!/usr/bin/env bash
set -euo pipefail

image_name="${1:-agent-liftoff}"

docker build -t "${image_name}" .
container_id="$(docker run --rm -d -p 127.0.0.1::8080 "${image_name}")"

cleanup() {
  docker stop "${container_id}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

published=""
for _attempt in $(seq 1 50); do
  published="$(docker port "${container_id}" 8080/tcp 2>/dev/null || true)"
  if [[ -n "${published}" ]]; then
    break
  fi
  sleep 0.2
done

if [[ -z "${published}" ]]; then
  docker logs "${container_id}"
  echo "Docker did not publish the Agent Liftoff port." >&2
  exit 1
fi

port="${published##*:}"
echo "Agent Liftoff: http://127.0.0.1:${port}"
echo "Press Ctrl-C to stop."
docker logs -f "${container_id}"
