// Syntax-checks every front-end script.
const { spawnSync } = require('child_process');
const fs = require('fs'), path = require('path');
const dir = path.join(__dirname, '..', 'frontend');
let bad = 0;
for (const f of fs.readdirSync(dir).filter(f => f.endsWith('.js'))) {
  const r = spawnSync(process.execPath, ['--check', path.join(dir, f)], { stdio: 'inherit' });
  if (r.status !== 0) bad++; else console.log('ok frontend/' + f);
}
process.exit(bad ? 1 : 0);
