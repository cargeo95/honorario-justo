const path = require('path');
const [input, modules, models] = process.argv.slice(2);
const { createWorker } = require(path.join(modules, 'tesseract.js'));
(async () => {
  const worker = await createWorker('spa', 1, { langPath: models, cachePath: models, logger: () => {} });
  try { const { data } = await worker.recognize(input); process.stdout.write(data.text); }
  finally { await worker.terminate(); }
})().catch(e => { process.stderr.write(String(e)); process.exitCode = 1; });
