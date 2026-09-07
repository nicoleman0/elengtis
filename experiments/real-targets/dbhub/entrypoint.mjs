import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, readFileSync } from 'node:fs';
import { spawn } from 'node:child_process';

mkdirSync('/tmp/dbhub', { recursive: true });
const db = new DatabaseSync('/tmp/dbhub/tickets.db');
db.exec(readFileSync('/opt/dbhub/seed.sql', 'utf8'));
db.close();
const child = spawn('/opt/dbhub/node_modules/.bin/dbhub', [
  '--config', '/opt/dbhub/dbhub.toml', '--transport', 'http', '--host', '0.0.0.0', '--port', '8080',
], { stdio: 'inherit' });
child.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
