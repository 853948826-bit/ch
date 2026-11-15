#!/usr/bin/env node
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const [command] = process.argv.slice(2);
if (!command || command === '--help' || command === '-h') {
  console.log('用法：cyber-clinic serve（需要 Python 3.9+，前台运行，Ctrl+C 停止）');
} else if (command === 'serve') {
  const python = process.platform === 'win32' ? 'python' : 'python3';
  const result = spawnSync(python, ['-m', 'clinic.guardian'], {
    cwd: path.resolve(__dirname, '..'), stdio: 'inherit',
  });
  if (result.error) console.error(`启动失败：${result.error.message}`);
  process.exitCode = result.status ?? (result.signal === 'SIGINT' ? 130 : 1);
} else {
  console.error('未知命令。请运行 cyber-clinic --help。');
  process.exitCode = 2;
}
