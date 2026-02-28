#!/usr/bin/env bash
# Record CLI GIFs using asciinema + agg
# Usage: ./record-cli.sh [recording-name]
# If no name given, records all.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS_DIR="$(cd "$SCRIPT_DIR/../../assets" && pwd)"
CAST_DIR="$SCRIPT_DIR/.casts"
mkdir -p "$CAST_DIR"

# agg settings for consistent look
AGG_OPTS=(
    --theme dracula
    --font-size 16
    --cols 120
    --rows 28
    --fps-cap 15
    --idle-time-limit 3
    --last-frame-duration 3
    --speed 1
)

# Simulates typing + running a command, with visible prompt
record_command() {
    local cast_file="$1"
    local gif_file="$2"
    shift 2
    local commands=("$@")

    echo "Recording: $gif_file"

    # Build the command script that simulates typing
    local script_file
    script_file=$(mktemp)

    cat > "$script_file" << 'PREAMBLE'
#!/usr/bin/env bash
# Typing simulator
type_command() {
    local cmd="$1"
    local delay="${2:-0.04}"
    printf '\n\033[1;32m❯\033[0m '
    for (( i=0; i<${#cmd}; i++ )); do
        printf '%s' "${cmd:$i:1}"
        sleep "$delay"
    done
    printf '\n'
}
PREAMBLE

    for cmd in "${commands[@]}"; do
        cat >> "$script_file" << EOF
type_command $(printf '%q' "$cmd")
$cmd
sleep 1
EOF
    done

    # Add final pause
    echo "sleep 2" >> "$script_file"

    chmod +x "$script_file"

    # Record with asciinema
    asciinema rec "$cast_file" \
        --overwrite \
        --cols 120 \
        --rows 28 \
        --command "bash $script_file" \
        --quiet

    rm -f "$script_file"

    # Convert to GIF
    agg "${AGG_OPTS[@]}" "$cast_file" "$gif_file"

    local size
    size=$(du -h "$gif_file" | cut -f1)
    echo "  -> $gif_file ($size)"
}

# --- Recording definitions ---

record_memory_search() {
    record_command \
        "$CAST_DIR/memory-search.cast" \
        "$ASSETS_DIR/memex_cli_memory.gif" \
        "memex memory search 'How does Python handle memory management?' -v demo-recordings"
}

record_memory_search_answer() {
    record_command \
        "$CAST_DIR/memory-search-answer.cast" \
        "$ASSETS_DIR/memex_cli_memory_answer.gif" \
        "memex memory search 'How does Python handle memory management?' --answer -v demo-recordings"
}

record_note_search_reason() {
    record_command \
        "$CAST_DIR/note-search-reason.cast" \
        "$ASSETS_DIR/memex_cli_docs.gif" \
        "memex note search 'vector similarity search' --reason -v demo-recordings"
}

record_entity_list() {
    record_command \
        "$CAST_DIR/entity-list.cast" \
        "$ASSETS_DIR/memex_cli_entities.gif" \
        "memex entity list" \
        "memex entity related Python"
}

record_stats_system() {
    record_command \
        "$CAST_DIR/stats-system.cast" \
        "$ASSETS_DIR/memex_cli_stats.gif" \
        "memex stats system"
}

record_memory_add_url() {
    record_command \
        "$CAST_DIR/memory-add-url.cast" \
        "$ASSETS_DIR/memex_cli_ingest.gif" \
        "memex memory add --url 'https://docs.python.org/3/tutorial/classes.html'"
}

# --- Main ---

ALL_RECORDINGS=(
    memory_search
    memory_search_answer
    note_search_reason
    entity_list
    stats_system
    memory_add_url
)

if [[ $# -eq 0 ]]; then
    echo "Recording all CLI GIFs..."
    for name in "${ALL_RECORDINGS[@]}"; do
        "record_${name}"
    done
    echo "Done. All GIFs saved to $ASSETS_DIR/"
else
    for name in "$@"; do
        normalized="${name//-/_}"
        if declare -f "record_${normalized}" > /dev/null 2>&1; then
            "record_${normalized}"
        else
            echo "Unknown recording: $name"
            echo "Available: ${ALL_RECORDINGS[*]}"
            exit 1
        fi
    done
fi
