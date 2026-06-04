import deckyPlugin from '@decky/rollup';
import { readFileSync } from 'fs';
import { extname, resolve, dirname } from 'path';

function inlineImages() {
    const PREFIX = '\0inline-image:';
    return {
        name: 'inline-images',
        resolveId(source, importer) {
            if (/\.(png|jpg|gif)$/.test(source)) {
                const filePath = importer
                    ? resolve(dirname(importer), source)
                    : resolve(source);
                return PREFIX + filePath;
            }
        },
        load(id) {
            if (id.startsWith(PREFIX)) {
                const filePath = id.slice(PREFIX.length);
                const ext  = extname(filePath).slice(1);
                const mime = ext === 'jpg' ? 'jpeg' : ext;
                const b64  = readFileSync(filePath).toString('base64');
                return `export default "data:image/${mime};base64,${b64}";`;
            }
        }
    };
}

const config = deckyPlugin();
config.plugins.unshift(inlineImages());
export default config;
