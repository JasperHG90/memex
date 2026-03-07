FROM docker.io/jhginn/devcontainer:ubuntu2204-pyuv312-latest-gcloud

# NB: vscode user is created in the base image
RUN mkdir -p /home/vscode/.cache && chown -R vscode:vscode /home/vscode/.cache
RUN mkdir -p /home/vscode/workspace && chown -R vscode:vscode /home/vscode/workspace

ENV RUFF_CACHE_DIR=$HOME/.cache/ruff
ENV PREK_HOME=$HOME/.cache/prek
ENV UV_CACHE_DIR="$HOME/.cache/uv"
ENV NVM_DIR="$HOME/.nvm"
ARG NODE_VERSION="22"
ENV PATH="${NVM_DIR}/versions/node/v${NODE_VERSION}/bin:${PATH}"

RUN pipx install --force uv==0.10.1

RUN apt-get update && \
    apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

USER vscode
WORKDIR /home/vscode

ENV NVM_DIR="/home/vscode/.nvm"
RUN touch /home/vscode/.bashrc

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash \
    && . "$NVM_DIR/nvm.sh" \
    && nvm install ${NODE_VERSION} \
    && nvm alias default ${NODE_VERSION} \
    && nvm use default \
    && npm install -g @google/gemini-cli

RUN echo '\nexport NVM_DIR="/home/vscode/.nvm"' >> /home/vscode/.bashrc && \
    echo '[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"  # This loads nvm' >> /home/vscode/.bashrc && \
    echo '[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"  # This loads nvm bash_completion' >> /home/vscode/.bashrc

ENV PATH="/home/vscode/.nvm/versions/node/v${NODE_VERSION}/bin:${PATH}"

# Install claude code
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install opencode
RUN curl -fsSL https://opencode.ai/install | bash

# Install bun
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="$HOME/.bun/bin:$PATH"

# Install qmd https://github.com/tobi/qmd
RUN bun install -g @tobilu/qmd
# Rebuild better-sqlite3 for the nvm Node.js version (bun compiles against its own)
#RUN cd /home/vscode/.bun/install/global/node_modules/better-sqlite3 && npm rebuild
# Fix qmd GPU detection: getLlamaGpuTypes() without "supported" arg always returns
# ["cuda","vulkan",false] on Linux regardless of hardware, causing a slow failed CUDA
# build on every vector search. Patch to pass "supported" so it only returns actually
# available GPU types.
RUN sed -i 's/await getLlamaGpuTypes()/await getLlamaGpuTypes("supported")/' \
    /home/vscode/.bun/install/global/node_modules/@tobilu/qmd/dist/llm.js

# Install rtk
RUN curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
ENV PATH="$HOME/.local/bin:$PATH"
