#!/bin/bash
# Build natively (arm64 on this Mac; timing only) and run under the scorer's flags.
# usage: tools/run_native.sh DATA_DIR [time]
set -e
DATA=$(cd "${1:-sample}" && pwd)
docker build -q -t br-native . >/dev/null
RUNF=(--rm --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only
      --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534
      --env HOME=/tmp -v "$DATA/commits:/data/commits:ro" -v "$DATA/tests.json:/data/tests.json:ro")
if [ "$2" = "time" ]; then
  for i in 1 2 3; do
    docker run "${RUNF[@]}" --entrypoint python br-native -c '
import subprocess,resource,time
t=time.time(); subprocess.run(["python","/app/selector.py"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
r=resource.getrusage(resource.RUSAGE_CHILDREN); print("cpu %.3f s wall %.3f s"%(r.ru_utime+r.ru_stime,time.time()-t))'
  done
else
  docker run "${RUNF[@]}" br-native
fi
