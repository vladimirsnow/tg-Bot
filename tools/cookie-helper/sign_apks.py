"""Align and sign release APKs without passing password values on the command line."""
import argparse,hashlib,subprocess
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--sdk',type=Path,required=True)
parser.add_argument('--keystore',type=Path,required=True)
parser.add_argument('--password-file',type=Path,required=True)
parser.add_argument('--output',type=Path,default=Path('artifacts'))
args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
root=Path(__file__).resolve().parents[2]
tools=args.sdk/'build-tools/36.0.0'
checks=[]
for abi,label in [('arm64-v8a','arm64'),('armeabi-v7a','armv7'),('x86_64','x86_64')]:
 source=root/f'android-cookie-helper/app/build/outputs/apk/release/app-{abi}-release-unsigned.apk'
 aligned=args.output/f'.{label}-aligned.apk'
 output=args.output/f'YouTube-Cookies-{label}-v1.0.0.apk'
 subprocess.run([str(tools/'zipalign'),'-P','16','-f','4',str(source),str(aligned)],check=True)
 subprocess.run([str(tools/'apksigner'),'sign','--ks',str(args.keystore),'--ks-key-alias','ytcookies','--ks-pass','file:'+str(args.password_file),'--out',str(output),str(aligned)],check=True)
 subprocess.run([str(tools/'apksigner'),'verify','--verbose',str(output)],check=True)
 aligned.unlink()
 checks.append(hashlib.sha256(output.read_bytes()).hexdigest()+'  '+output.name)
 print('SIGNED',output.name,round(output.stat().st_size/1024/1024,1),'MiB')
(args.output/'SHA256SUMS.txt').write_text('\n'.join(checks)+'\n')
