import {convertCookies} from './codec.js';
const base = '# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\ttest\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tapi\n';
Deno.test('export filters foreign domains and round-trips base64', () => {
 const e=convertCookies(base+'.google.com\tTRUE\t/\tTRUE\t2147483647\tOTHER\tsecret\n');
 if(e.count!==2 || e.netscape.includes('secret') || atob(e.base64)!==e.netscape || !e.env.startsWith('YOUTUBE_COOKIES_BASE64='))throw new Error('Invalid export');
});
Deno.test('rejects unsigned session and malformed Netscape rows', () => {
 for(const text of ['{}',base+'broken\n',base.replaceAll('.youtube.com','.youtube.com.evil.invalid')]){
  let rejected=false;try{convertCookies(text);}catch{rejected=true;}if(!rejected)throw new Error('Unsafe input accepted');
 }
});
Deno.test('drops expired cookies but keeps session expiration zero', () => {
 const e=convertCookies(base+'.youtube.com\tTRUE\t/\tTRUE\t1\tOLD\tvalue\n.youtube.com\tTRUE\t/\tTRUE\t0\tSESSION\tvalue\n');
 if(e.count!==3 || e.netscape.includes('\tOLD\t') || !e.netscape.includes('TRUE\t\tSESSION\tvalue'))throw new Error('Incorrect expiration handling');
});
