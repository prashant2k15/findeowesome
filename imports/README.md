Drop `.txt`, `.csv`, `.md` or `.list` files here.

The `import` job (every 15 minutes) extracts every URL it can find, adds the
new ones to the master database, and renames the file to `*.done` so it is not
read twice.
