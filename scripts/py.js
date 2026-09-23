// Runs a Python script with whichever Python 3 is installed (python3, python or py).
const { spawnSync } = require('child_process');
const args = process.argv.slice(2);
for (const exe of ['python3', 'python', 'py']) {
  const r = spawnSync(exe, args, { stdio: 'inherit' });
  if (r.error && r.error.code === 'ENOENT') continue;
  if (r.status === 9009) continue; // Windows "python" shortcut that only opens the Microsoft Store
  process.exit(r.status ?? 1);
}
console.error('Python 3 is required. Install it from https://www.python.org/downloads/');
process.exit(1);
