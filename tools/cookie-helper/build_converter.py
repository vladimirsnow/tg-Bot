"""Build one downloadable HTML file with no remote resources or network access."""
from pathlib import Path
import hashlib,base64
root=Path(__file__).parent
codec=(root/'codec.js').read_text().replace('export function ','function ')
style='''body{font:17px system-ui,sans-serif;line-height:1.5;margin:0;background:#f4f6fa;color:#10243a}main{max-width:650px;margin:32px auto;padding:24px;background:white;border-radius:20px}h1{font-size:28px;margin-top:0}button,input{font:inherit}button{border:0;background:#1b62d1;color:white;padding:14px 18px;border-radius:10px;margin:8px 6px 0 0;cursor:pointer}button:disabled{opacity:.45;cursor:default}button:focus-visible,input:focus-visible{outline:3px solid #8ab8ff;outline-offset:3px}input{display:block;width:100%;padding:16px 0}small{display:block;margin-top:22px}#status{min-height:50px;background:#eef3fb;border-radius:12px;padding:14px}ol{padding-left:24px}@media(max-width:700px){main{margin:0;border-radius:0;padding:20px}button{width:100%;margin-right:0}}'''
script=codec+'''
let result=null;
const $=id=>document.getElementById(id);
function reset(){result=null;$('file').value='';$('copy').disabled=$('save').disabled=true;$('status').textContent='Выбери cookies.txt. Содержимое не отправляется на сервер.';}
$('file').addEventListener('change',async()=>{try{const file=$('file').files[0];if(!file)return;if(file.size>131072)throw new Error('Файл больше 128 КБ.');result=convertCookies(await file.text());$('status').textContent='Готово: '+result.count+' cookies YouTube. Остальные сайты исключены.';$('copy').disabled=$('save').disabled=false;}catch(e){result=null;$('copy').disabled=$('save').disabled=true;$('status').textContent=e.message;}});
$('copy').addEventListener('click',async()=>{if(!result)return;try{await navigator.clipboard.writeText(result.base64);}catch{const box=document.createElement('textarea');box.value=result.base64;box.style.position='fixed';box.style.opacity='0';document.body.append(box);box.select();const ok=document.execCommand('copy');box.remove();if(!ok){$('status').textContent='Браузер не дал доступ к буферу. Скачай .env и импортируй его в Render.';return;}}$('status').textContent='Значение скопировано. Вставь его в YOUTUBE_COOKIES_BASE64 в Render → Environment.';});
$('save').addEventListener('click',()=>{if(!result)return;const url=URL.createObjectURL(new Blob([result.env],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='render-youtube.env';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
$('clear').addEventListener('click',reset);
reset();
'''
def digest(value):return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()
csp=f"default-src 'none'; script-src 'sha256-{digest(script)}'; style-src 'sha256-{digest(style)}'; connect-src 'none'; base-uri 'none'; form-action 'none'"
html=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{csp}"><title>Cookies YouTube для Render</title><style>{style}</style><main><h1>Cookies YouTube → Render</h1><p>Локальный конвертер. Работает без интернета и не загружает cookies на сервер.</p><ol><li>Экспортируй cookies YouTube в Netscape TXT и закрой отдельную сессию браузера.</li><li>Выбери файл здесь и скопируй значение.</li><li>В Render → Environment замени <strong>YOUTUBE_COOKIES_BASE64</strong> и нажми <strong>Save, rebuild, and deploy</strong>.</li></ol><label for="file">Файл cookies.txt</label><input id="file" type="file" accept=".txt,text/plain"><p id="status" role="status" aria-live="polite"></p><button id="copy" disabled>Копировать значение</button><button id="save" disabled>Скачать .env</button><button id="clear">Очистить</button><small>Cookies дают доступ к сессии. Не публикуй TXT, .env или скопированное значение и не отправляй их в Telegram.</small></main><script>{script}</script></html>'''
out=root/'cookies-render.html';out.write_text(html)
print('Offline converter built:',out.name,'bytes',out.stat().st_size)
