import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

const HAN_TEXT = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/u

function enforceEnglishStaticAssets() {
  return {
    name: 'enforce-english-static-assets',
    enforce: 'pre',
    transform(code, id) {
      const normalizedId = id.replaceAll('\\', '/')
      if (!normalizedId.endsWith('/element-plus/dist/index.css')) return null

      // The English font family is already listed immediately before this
      // vendor alias, so removing the alias does not change font selection.
      return {
        code: code.replace(/,\s*"\u5fae\u8f6f\u96c5\u9ed1"/gu, ''),
        map: null,
      }
    },
    generateBundle(_options, bundle) {
      for (const [fileName, output] of Object.entries(bundle)) {
        const content = output.type === 'chunk'
          ? output.code
          : typeof output.source === 'string'
            ? output.source
            : new TextDecoder().decode(output.source)
        if (HAN_TEXT.test(content)) {
          this.error(`Non-English Han text remains in generated asset: ${fileName}`)
        }
      }
    },
  }
}

// Write production assets to ../static for FastAPI to serve. In development,
// proxy /api to the local backend. Element Plus JavaScript is tree-shaken while
// its complete CSS bundle remains available for programmatic components.
export default defineConfig({
  plugins: [
    enforceEnglishStaticAssets(),
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver({ importStyle: false })] }),
    Components({ resolvers: [ElementPlusResolver({ importStyle: false })] }),
  ],
  // FastAPI mounts assets under /static and serves index.html from /.
  base: '/static/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5666,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
})
