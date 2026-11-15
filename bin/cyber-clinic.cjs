#!/usr/bin/env node
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const [command] = process.argv.slice(2);

function port() {
  try {
    return JSON.parse(fs.readFileSync(path.join(root, 'config.json'), 'utf8')).port || 8765;
  } catch {
    return 8765;
  }
}

function serve() {
  const python = process.platform === 'win32' ? 'python' : 'python3';
  const result = spawnSync(python, ['-m', 'clinic.guardian'], { cwd: root, stdio: 'inherit' });
  if (result.error) console.error(`启动失败：${result.error.message}（需要 Python 3.9+，并已加入 PATH）`);
  process.exitCode = result.status ?? (result.signal === 'SIGINT' ? 130 : 1);
}

if (!command || command === '--help' || command === '-h') {
  console.log('用法：cyber-clinic serve（需要 Python 3.9+，前台运行，Ctrl+C 停止）');
} else if (command === 'serve') {
  const p = port();
  const probe = net.createServer();
  probe.once('error', (e) => {
    console.error(e.code === 'EADDRINUSE'
      ? `端口 ${p} 已被占用：可能赛博诊所已在运行（桌面版或另一个 serve），请先退出它；或把 ${path.join(root, 'config.json')} 里的 port 改成其他端口。`
      : `无法监听端口 ${p}：${e.message}`);
    process.exitCode = 1;
  });
  probe.once('listening', () => probe.close(serve));
  probe.listen(p, '127.0.0.1');
} else {
  console.error('未知命令。请运行 cyber-clinic --help。');
  process.exitCode = 2;
}
