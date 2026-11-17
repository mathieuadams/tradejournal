// Syntax-checks every front-end script.
const { spawnSync } = require('child_process');
const fs = require('fs'), path = require('path');
let bad = 0;
for (const rel of ['frontend', 'workspace/web']) {
  const dir = path.join(__dirname, '..', rel);
  for (const f of fs.readdirSync(dir).filter(f => f.endsWith('.js'))) {
    const r = spawnSync(process.execPath, ['--check', path.join(dir, f)], { stdio: 'inherit' });
    if (r.status !== 0) bad++; else console.log(`ok ${rel}/${f}`);
  }
}
process.exit(bad ? 1 : 0);
