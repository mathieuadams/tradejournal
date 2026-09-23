// Copies the web app into dist/ (what gets uploaded to S3). No bundling is needed.
const fs = require('fs'), path = require('path');
const src = path.join(__dirname, '..', 'frontend'), out = path.join(__dirname, '..', 'dist');
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out);
for (const f of fs.readdirSync(src)) fs.copyFileSync(path.join(src, f), path.join(out, f));
console.log('Built dist/ with ' + fs.readdirSync(out).length + ' files');
