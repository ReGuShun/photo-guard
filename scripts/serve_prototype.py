"""Serve the static prototype; no model environment required."""
import argparse
import functools
import http.server
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=6007)
args = parser.parse_args()
directory = Path(__file__).resolve().parents[1] / 'prototype'
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
print(f'Open http://localhost:{args.port}', flush=True)
http.server.ThreadingHTTPServer(('0.0.0.0', args.port), handler).serve_forever()
