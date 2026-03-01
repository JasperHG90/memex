#!/usr/bin/env node
/**
 * Production server for the Memex Dashboard.
 *
 * Serves the static build from ./dist and reverse-proxies /api/* and /metrics
 * requests to the Memex Core API server.
 *
 * Usage: node serve.cjs --port 3001 --host 0.0.0.0 --api http://localhost:8000
 */

'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = { port: 3001, host: '0.0.0.0', api: 'http://localhost:8000' };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--port' && argv[i + 1]) args.port = Number(argv[++i]);
    else if (argv[i] === '--host' && argv[i + 1]) args.host = argv[++i];
    else if (argv[i] === '--api' && argv[i + 1]) args.api = argv[++i];
  }
  return args;
}

// ---------------------------------------------------------------------------
// MIME types
// ---------------------------------------------------------------------------

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.map': 'application/json',
};

// ---------------------------------------------------------------------------
// Reverse proxy
// ---------------------------------------------------------------------------

function proxyRequest(req, res, apiOrigin) {
  const parsed = new url.URL(apiOrigin);
  const options = {
    hostname: parsed.hostname,
    port: parsed.port || 80,
    path: req.url,
    method: req.method,
    headers: { ...req.headers, host: parsed.host },
  };

  const proxyReq = http.request(options, (proxyRes) => {
    res.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(res, { end: true });
  });

  proxyReq.on('error', (err) => {
    console.error(`Proxy error: ${err.message}`);
    if (!res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'application/json' });
    }
    res.end(JSON.stringify({ detail: 'API server unavailable' }));
  });

  req.pipe(proxyReq, { end: true });
}

// ---------------------------------------------------------------------------
// Static file serving (with SPA fallback)
// ---------------------------------------------------------------------------

const DIST = path.join(__dirname, 'dist');

function serveStatic(req, res) {
  const parsedUrl = new url.URL(req.url, 'http://localhost');
  let pathname = path.normalize(decodeURIComponent(parsedUrl.pathname));

  // Resolve to a file in dist/
  let filePath = path.join(DIST, pathname);

  // Security: prevent path traversal
  if (!filePath.startsWith(DIST)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  // Try the exact path, then fall back to index.html (SPA routing)
  fs.stat(filePath, (err, stats) => {
    if (!err && stats.isFile()) {
      sendFile(filePath, res);
    } else if (!err && stats.isDirectory()) {
      // Try index.html inside directory
      const indexPath = path.join(filePath, 'index.html');
      fs.stat(indexPath, (err2) => {
        if (!err2) sendFile(indexPath, res);
        else spaFallback(res);
      });
    } else {
      spaFallback(res);
    }
  });
}

function spaFallback(res) {
  sendFile(path.join(DIST, 'index.html'), res);
}

function sendFile(filePath, res) {
  const ext = path.extname(filePath).toLowerCase();
  const contentType = MIME[ext] || 'application/octet-stream';

  // Cache static assets aggressively (hashed filenames), HTML never
  const cacheControl = ext === '.html'
    ? 'no-cache'
    : 'public, max-age=31536000, immutable';

  const stream = fs.createReadStream(filePath);
  stream.on('open', () => {
    res.writeHead(200, {
      'Content-Type': contentType,
      'Cache-Control': cacheControl,
    });
    stream.pipe(res);
  });
  stream.on('error', () => {
    res.writeHead(500);
    res.end('Internal Server Error');
  });
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

const PROXY_PREFIXES = ['/api/', '/metrics'];

const args = parseArgs(process.argv);

const server = http.createServer((req, res) => {
  if (PROXY_PREFIXES.some((p) => req.url.startsWith(p))) {
    proxyRequest(req, res, args.api);
  } else {
    serveStatic(req, res);
  }
});

server.listen(args.port, args.host, () => {
  console.log(`Dashboard serving on http://${args.host}:${args.port}`);
  console.log(`API proxy target: ${args.api}`);
});
