// node raw 输入靶子：记录收到的字节
const fs = require('fs');
const out = 'E:/1workspace/claude-remote-web/data/node_keys.log';
fs.writeFileSync(out, '');
process.stdin.setRawMode(true);
process.stdin.resume();
process.stdin.on('data', (d) => fs.appendFileSync(out, JSON.stringify([...d]) + '\n'));
console.log('NODE_TARGET_READY', process.pid);
