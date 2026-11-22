#!/bin/sh
# usage: tools/eval.sh <image> <set dir with commits/ and answers.json> <name> [extra docker args]
# Runs the selector under the scorer's limits on one labelled set and scores it.
IMAGE=$1; SET=$(cd "$2" && pwd); NAME=$3; shift 3
HERE=$(cd "$(dirname "$0")/.." && pwd)
TESTS=$SET/tests.json; [ -f "$TESTS" ] || TESTS=$HERE/sample/tests.json
mkdir -p "$HERE/out"
START=$(date +%s)
docker run --rm --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 \
  --env HOME=/tmp --volume "$SET/commits:/data/commits:ro" --volume "$TESTS:/data/tests.json:ro" "$@" "$IMAGE" \
  > "$HERE/out/$NAME.jsonl" 2> "$HERE/out/$NAME.err"
echo "$NAME: exit $? wall $(( $(date +%s) - START ))s cpu-sum $(grep -o 'cpu [0-9.]*s' "$HERE/out/$NAME.err" | awk '{s+=$2} END {print s}')"
mkdir -p "$HERE/out/data_$NAME" && rm -rf "$HERE/out/data_$NAME/commits" && cp -r "$SET/commits" "$HERE/out/data_$NAME/" && cp "$TESTS" "$HERE/out/data_$NAME/tests.json"
python3 "$HERE/score.py" "$HERE/out/data_$NAME" "$SET/answers.json" "$HERE/out/$NAME.jsonl" | head -8
