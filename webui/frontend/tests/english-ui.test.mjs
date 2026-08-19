import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import test from 'node:test'

const HAN_TEXT = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/u

async function listFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const nested = await Promise.all(entries.map((entry) => {
    const url = new URL(entry.name, directory)
    return entry.isDirectory() ? listFiles(new URL(`${entry.name}/`, directory)) : [url]
  }))
  return nested.flat()
}


test('Element Plus uses its English locale', async () => {
  const app = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')

  assert.match(app, /locale\/lang\/en'/)
  assert.match(app, /:locale="en"/)
  assert.doesNotMatch(app, /zh-cn|zhCn/i)
})

test('shipped production static assets contain no Han text', async () => {
  const staticDirectory = new URL('../../static/', import.meta.url)
  const files = await listFiles(staticDirectory)

  for (const file of files.filter((item) => /\.(?:css|html|js|json|svg|txt)$/i.test(item.pathname))) {
    const content = await readFile(file, 'utf8')
    assert.doesNotMatch(content, HAN_TEXT, file.pathname)
  }
})
