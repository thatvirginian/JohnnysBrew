import os

# Bind to port 8080 (Azure Container Apps default standard)
bind = "0.0.0.0:8080"

# Formula: (2 x number of cores) + 1. For a standard small ACA ingress container, 2 workers is plenty.
workers = 2

# Use threads to handle I/O bound database queries efficiently without multiplying memory overhead
threads = 4

# Timeout limits to handle larger Excel exports safely without dropping the worker connection
timeout = 120

# Keep logs writing straight to stdout/stderr so Azure Log Analytics captures them instantly
accesslog = "-"
errorlog = "-"
loglevel = "info"
