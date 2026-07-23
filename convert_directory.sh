#!/usr/bin/env bash
#
# Converts every CSV file in a directory to a MoTeC .ld file.
#
# Usage: ./convert_directory.sh <input_dir> [output_dir] [-- extra args for motec_log_generator.py]
#
# The log type defaults to CSV, pass "--log_type ACCESSPORT" after "--" for accessport logs. The
# files to convert default to *.csv, pass "--ext log" after "--" for delimited logs which use a
# different extension. Any other arguments after "--" are passed straight through to
# motec_log_generator.py, e.g.
#
#   ./convert_directory.sh ./logs ./out -- --frequency 50 --vehicle_type "Cayman S"
#   ./convert_directory.sh ./data2 ./out -- --ext log

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATOR="$SCRIPT_DIR/motec_log_generator.py"

if [ $# -lt 1 ]; then
    sed -n '3,13p' "${BASH_SOURCE[0]}" | cut -c 3-
    exit 1
fi

INPUT_DIR="$1"
shift

# The output directory is optional, anything after "--" is for the generator
OUTPUT_DIR=""
if [ $# -gt 0 ] && [ "$1" != "--" ]; then
    OUTPUT_DIR="$1"
    shift
fi
if [ $# -gt 0 ] && [ "$1" == "--" ]; then
    shift
fi

# Pull the log type and file extension out of the remaining arguments, everything else goes to the
# generator as is
LOG_TYPE="CSV"
EXTENSION="csv"
GENERATOR_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --log_type)
            LOG_TYPE="$2"
            shift 2
            ;;
        --ext)
            EXTENSION="${2#.}"
            shift 2
            ;;
        *)
            GENERATOR_ARGS+=("$1")
            shift
            ;;
    esac
done

if [ ! -d "$INPUT_DIR" ]; then
    echo "ERROR: input directory '$INPUT_DIR' does not exist"
    exit 1
fi

if [ -n "$OUTPUT_DIR" ]; then
    mkdir -p "$OUTPUT_DIR"
fi

converted=0
failed=0
failures=()

# Loop over the CSV files, handling any spaces in the file names. The log output from each
# conversion is kept in a temporary file so only a summary line is printed unless it fails.
log_output="$(mktemp)"
trap 'rm -f "$log_output"' EXIT

while IFS= read -r -d '' csv_file; do
    name="$(basename "$csv_file")"

    output_args=()
    if [ -n "$OUTPUT_DIR" ]; then
        output_args=(--output "$OUTPUT_DIR/${name%.*}.ld")
    fi

    if python3 "$GENERATOR" "$csv_file" "$LOG_TYPE" "${output_args[@]}" \
        ${GENERATOR_ARGS[@]+"${GENERATOR_ARGS[@]}"} > "$log_output" 2>&1; then
        # Report the channel count and duration from the generator output
        summary="$(grep -m 1 "^Parsed" "$log_output")"
        summary="${summary#Parsed }"
        echo "OK    $name    ${summary%:}"
        converted=$((converted + 1))
    else
        echo "FAIL  $name"
        sed 's/^/          /' "$log_output"
        failures+=("$name")
        failed=$((failed + 1))
    fi
done < <(find "$INPUT_DIR" -maxdepth 1 -type f -iname "*.$EXTENSION" -print0 | sort -z)

echo
echo "Converted $converted file(s), $failed failed"
for name in ${failures[@]+"${failures[@]}"}; do
    echo "    failed: $name"
done

[ "$failed" -eq 0 ]
