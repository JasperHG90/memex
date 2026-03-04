#!/usr/bin/env bash
# Record a simulated Claude Code + Memex integration GIF
# This doesn't run a real Claude Code session — it renders a scripted
# terminal animation captured by asciinema and converted to GIF with agg.
#
# Usage: bash recordings/cli/record-claude-code.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS_DIR="$(cd "$SCRIPT_DIR/../../assets" && pwd)"
CAST_DIR="$SCRIPT_DIR/.casts"
mkdir -p "$CAST_DIR"

CAST_FILE="$CAST_DIR/claude-code-memex.cast"
GIF_FILE="$ASSETS_DIR/memex_claude_code.gif"

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

echo "Recording: $GIF_FILE"

# Build the simulation script
SCRIPT_FILE=$(mktemp)
cat > "$SCRIPT_FILE" << 'SIMEOF'
#!/usr/bin/env bash
# Simulated Claude Code + Memex session

# --- Colors (Dracula palette) ---
PURPLE='\033[38;5;141m'
CYAN='\033[38;5;117m'
GREEN='\033[38;5;84m'
YELLOW='\033[38;5;228m'
WHITE='\033[1;37m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

# --- Helpers ---

type_text() {
    local text="$1"
    local delay="${2:-0.035}"
    for (( i=0; i<${#text}; i++ )); do
        printf '%s' "${text:$i:1}"
        sleep "$delay"
    done
}

stream_words() {
    local text="$1"
    local delay="${2:-0.03}"
    local first=true
    for word in $text; do
        if $first; then
            first=false
        else
            printf ' '
        fi
        printf '%s' "$word"
        sleep "$delay"
    done
}

show_prompt() {
    printf '\n'
    printf "${DIM}claude${RESET}  ${CYAN}~/memex${RESET} ${DIM}(main)${RESET} ${PURPLE}\$${RESET} "
}

show_user_input() {
    show_prompt
    printf '\n'
    printf "  ${WHITE}>${RESET} "
    type_text "$1" 0.035
    printf '\n'
}

show_tool_call() {
    printf "    ${CYAN}● ${1}${RESET}"
    printf "${DIM}(${RESET}"
    stream_words "$2" 0.02
    printf "${DIM})${RESET}\n"
    sleep 0.8
}

show_thinking() {
    printf "    ${DIM}⠋ Thinking...${RESET}"
    sleep 0.6
    printf "\r    ${DIM}⠙ Thinking...${RESET}"
    sleep 0.4
    printf "\r                    \r"
}

show_response_line() {
    printf "  "
    stream_words "$1" 0.03
    printf '\n'
}

show_bullet() {
    printf "  ${DIM}•${RESET} "
    stream_words "$1" 0.025
    printf '\n'
}

show_save_success() {
    printf "    ${GREEN}✓${RESET} "
    stream_words "$1" 0.02
    printf '\n'
}

show_memory_result() {
    local score="$1"
    local timestamp="$2"
    local text="$3"
    printf "    ${YELLOW}${score}${RESET}  ${DIM}${timestamp}${RESET}  ${text}\n"
    sleep 0.15
}

# === Scene 1 — Memory Retrieval ===
sleep 0.5
show_user_input "What were we working on last session?"
show_thinking
show_tool_call "memex_memory_search" 'query="previous session work"'
printf '\n'
show_response_line "Based on your memories from the last session, you were working on:"
printf '\n'
show_bullet "Migrating the vector store from pgvector to a hybrid HNSW + IVFFlat index"
show_bullet "Fixing connection pool exhaustion during concurrent reflection tasks"
show_bullet "Adding batch ingestion support for large PDF collections"
printf '\n'
show_response_line "The migration was about 70% complete. Want to pick up where you left off?"
sleep 2.5

# === Scene 2 — Automatic Save ===
show_user_input "Let's switch the embedding model from all-MiniLM-L6 to nomic-embed-text"
show_thinking
show_response_line "Good call. nomic-embed-text has better performance on retrieval benchmarks"
show_response_line "and supports longer context windows (8192 vs 256 tokens). I'll update the"
show_response_line "config and re-embed affected collections."
printf '\n'
sleep 0.5
show_tool_call "memex_add_note" 'title="Decision: Switch to nomic-embed-text"'
show_save_success "Saved to memory"
sleep 2.5

# === Scene 3 — Slash Commands ===
show_user_input "/recall connection pool fix"
show_tool_call "memex_memory_search" 'query="connection pool fix"'
printf '\n'
show_memory_result "0.92" "2h ago " "ReflectionWorker now uses pool.acquire() instead of direct connect()"
show_memory_result "0.87" "2h ago " "Max pool size set to 20 for production workloads"
show_memory_result "0.81" "1d ago " "Connection leak traced to unclosed cursors in batch ingestion"
printf '\n'
sleep 2.0

show_user_input "/remember Always run VACUUM ANALYZE after bulk embedding updates"
show_tool_call "memex_add_note" 'title="Config: Post-embedding maintenance"'
show_save_success "Saved to memory"
sleep 2
SIMEOF

chmod +x "$SCRIPT_FILE"

# Record with asciinema
asciinema rec "$CAST_FILE" \
    --overwrite \
    --cols 120 \
    --rows 28 \
    --command "bash $SCRIPT_FILE" \
    --quiet

rm -f "$SCRIPT_FILE"

# Convert to GIF
agg "${AGG_OPTS[@]}" "$CAST_FILE" "$GIF_FILE"

SIZE=$(du -h "$GIF_FILE" | cut -f1)
echo "  -> $GIF_FILE ($SIZE)"
