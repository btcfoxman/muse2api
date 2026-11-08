"""Select direct/proxy by real layer transfer; import an immutable verified image.

Uses a checksum-pinned prebuilt crane, never builds or installs dependencies.
Docker daemon proxy settings are left untouched; crane obeys its process env.
"""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TOOL_URL='https://github.com/google/go-containerregistry/releases/download/v0.20.3/go-containerregistry_Linux_x86_64.tar.gz'
TOOL_SHA='36c67a932f489b3f2724b64af90b599a8ef2aa7b004872597373c0ad694dc059'
ACCEPT=', '.join(('application/vnd.oci.image.index.v1+json','application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json','application/vnd.docker.distribution.manifest.v2+json'))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def command(args,env=None,timeout=30,stage='docker_operation'):
    if not re.fullmatch(r'[a-z_]{1,48}',stage):raise ValueError('invalid operation stage')
    try:result=subprocess.run(args,env=env,capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired:raise RuntimeError('verified_image_timeout:'+stage)from None
    except OSError:raise RuntimeError('verified_image_unavailable:'+stage)from None
    if result.returncode:raise RuntimeError('verified_image_failed:'+stage+':exit='+str(result.returncode))
    return result.stdout

def route_environment(proxy):
    env={key:value for key,value in os.environ.items()if key.upper()not in{'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'}}
    if proxy:env.update(HTTP_PROXY=proxy,HTTPS_PROXY=proxy)
    else:env.update(HTTP_PROXY='',HTTPS_PROXY='',ALL_PROXY='',NO_PROXY='*')
    return env

def transport(proxy):
    return urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy}if proxy else{}),NoRedirect())

def response(url,proxy,headers=None,timeout=10):
    headers=dict(headers or{})
    client=transport(proxy)
    for _ in range(5):
        parsed=urllib.parse.urlsplit(url)
        if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in(None,443)
            or not(parsed.hostname in{'ghcr.io','github.com'}or str(parsed.hostname).endswith('.githubusercontent.com'))):
            raise RuntimeError('unexpected_artifact_redirect')
        try:return client.open(urllib.request.Request(url,headers=headers),timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code not in{301,302,303,307,308}:error.close();raise RuntimeError('registry_http_rejected')from None
            target=urllib.parse.urljoin(url,error.headers.get('Location',''));error.close()
            if urllib.parse.urlsplit(target).hostname!=parsed.hostname:headers.pop('Authorization',None)
            url=target
    raise RuntimeError('artifact_redirect_budget_exhausted')

def read_bounded(url,proxy,headers=None,limit=2097152):
    with response(url,proxy,headers)as stream:
        value=stream.read(limit+1)
    if len(value)>limit:raise RuntimeError('registry_metadata_too_large')
    return value

def configured_proxy():
    raw=command(['docker','info','--format','{{json .HTTPSProxy}}'],stage='docker_proxy_info').decode().strip()
    proxy=json.loads(raw)if raw else''
    if not proxy:
        raw=command(['docker','info','--format','{{json .HTTPProxy}}'],stage='docker_proxy_info').decode().strip()
        proxy=json.loads(raw)if raw else''
    if not proxy:return''
    parsed=urllib.parse.urlsplit(proxy)
    if parsed.scheme not in{'http','https'}or not parsed.hostname or not parsed.port:raise RuntimeError('deployment_proxy_invalid')
    return proxy

def basic_auth():
    path=Path(os.environ.get('DOCKER_CONFIG')or str(Path.home()/'.docker'))/'config.json'
    config=json.loads(path.read_text())
    for host in('ghcr.io','https://ghcr.io','https://ghcr.io/v1/'):
        value=(config.get('auths',{}).get(host)or{}).get('auth')
        if isinstance(value,str)and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}',value):return value
    raise RuntimeError('isolated_registry_login_required')

def sample(url,proxy,headers):
    size=1048576;start=time.monotonic();got=0
    def expired(*_):raise TimeoutError()
    previous=signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,12)
    try:
        with response(url,proxy,{**headers,'Range':'bytes=0-1048575','Accept-Encoding':'identity'},timeout=8)as stream:
            if stream.status!=206:raise RuntimeError('range_not_supported')
            while got<size:
                chunk=stream.read(min(65536,size-got))
                if not chunk:break
                got+=len(chunk)
        good=got==size
    except Exception:good=False
    finally:signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous)
    seconds=time.monotonic()-start
    return{'route':'proxy'if proxy else'direct','complete':good,'bytes':got,'seconds':round(seconds,3),
        'bytes_per_second':round(got/max(seconds,.001),2)}

def acquire_tool(folder,routes,cache):
    cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    archive=cache/('crane-v0.20.3-'+TOOL_SHA[:16]+'.tar.gz')
    if not archive.exists()or hashlib.sha256(archive.read_bytes()).hexdigest()!=TOOL_SHA:
        for proxy in routes:
            temporary=folder/'crane.download'
            try:
                start=time.monotonic();total=0
                with response(TOOL_URL,proxy,timeout=8)as source,temporary.open('wb')as target:
                    while True:
                        chunk=source.read(131072)
                        if not chunk:break
                        total+=len(chunk)
                        if total>32*1024*1024 or time.monotonic()-start>75:raise RuntimeError('binary_download_budget_exhausted')
                        target.write(chunk)
                if hashlib.sha256(temporary.read_bytes()).hexdigest()!=TOOL_SHA:raise RuntimeError('crane_checksum_mismatch')
                os.replace(temporary,archive);archive.chmod(0o600);break
            except Exception:temporary.unlink(missing_ok=True)
        else:raise RuntimeError('verified_crane_download_unavailable')
    packed=archive.read_bytes()
    if hashlib.sha256(packed).hexdigest()!=TOOL_SHA:raise RuntimeError('crane_checksum_mismatch')
    with tarfile.open(fileobj=io.BytesIO(packed),mode='r:gz')as bundle:
        entry=bundle.getmember('crane')
        if not entry.isfile()or not 0<entry.size<=64*1024*1024:raise RuntimeError('crane_archive_invalid')
        binary=folder/'crane';binary.write_bytes(bundle.extractfile(entry).read());binary.chmod(0o700)
    return binary

def pull(binary,reference,archive,proxy):
    with tempfile.TemporaryFile()as log:
        process=subprocess.Popen([str(binary),'pull',reference,str(archive),'--platform','linux/amd64'],
            env=route_environment(proxy),stdout=subprocess.DEVNULL,stderr=log)
        start=time.monotonic();last=start;last_size=0
        try:
            while process.poll()is None:
                time.sleep(2);current=time.monotonic()
                size=archive.stat().st_size if archive.exists()else 0
                if current-start>900:raise RuntimeError('image_download_budget_exhausted')
                if current-last>=45:
                    if size-last_size<45*32768:raise RuntimeError('image_download_route_too_slow')
                    last,last_size=current,size
            if process.returncode:raise RuntimeError('crane_pull_failed')
        finally:
            if process.poll()is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

def check_disk_budget(root,compressed,image_size):
    docker_root=Path(command(['docker','info','--format','{{.DockerRootDir}}'],stage='docker_storage_info').decode().strip())
    if not docker_root.is_absolute() or not docker_root.is_dir():raise RuntimeError('docker_storage_location_unavailable')
    if not isinstance(image_size,int) or image_size<=0:raise RuntimeError('image_size_required')
    # The archive stays compressed, while Docker may stage and unpack layers.
    archive_budget=compressed+64*1024*1024
    docker_budget=image_size*2+512*1024*1024
    if root.stat().st_dev==docker_root.stat().st_dev:
        sufficient=shutil.disk_usage(root).free>=archive_budget+docker_budget
    else:
        sufficient=(shutil.disk_usage(root).free>=archive_budget and
                    shutil.disk_usage(docker_root).free>=docker_budget)
    if not sufficient:raise RuntimeError('image_import_disk_budget_unavailable')

MAX_EXPANDED_BYTES=16*1024*1024*1024
MAX_VALIDATION_SECONDS=180
GZIP_LAYERS={'application/vnd.oci.image.layer.v1.tar+gzip',
    'application/vnd.docker.image.rootfs.diff.tar.gzip'}
RAW_LAYERS={'application/vnd.oci.image.layer.v1.tar',
    'application/vnd.docker.image.rootfs.diff.tar'}

def validate_config(raw,descriptor,revision):
    if ('sha256:'+hashlib.sha256(raw).hexdigest()!=descriptor['digest']
            or len(raw)!=descriptor['size']):raise RuntimeError('registry_config_digest_mismatch')
    config=json.loads(raw)
    if config.get('os')!='linux'or config.get('architecture')!='amd64':raise RuntimeError('image_config_platform_mismatch')
    runtime=config.get('config')
    if not isinstance(runtime,dict):raise RuntimeError('image_config_invalid')
    if (runtime.get('Labels')or{}).get('org.opencontainers.image.revision')!=revision:
        raise RuntimeError('image_config_revision_mismatch')
    rootfs=config.get('rootfs')or{}
    if rootfs.get('type')!='layers'or not isinstance(rootfs.get('diff_ids'),list):raise RuntimeError('image_rootfs_invalid')
    if not all(re.fullmatch(r'sha256:[a-f0-9]{64}',str(value))for value in rootfs['diff_ids']):
        raise RuntimeError('image_rootfs_digest_invalid')
    return config

def validate_archive(archive,manifest,config_raw,config,temporary_tag):
    """Verify compressed digests and uncompressed diff_ids without extracting files."""
    started=time.monotonic();expanded=0
    layers=manifest['layers'];diff_ids=config['rootfs']['diff_ids']
    if not 0<len(layers)<=256 or len(layers)!=len(diff_ids):raise RuntimeError('image_layer_count_mismatch')
    compressed=sum(row['size']for row in layers)
    if archive.stat().st_size>compressed+64*1024*1024:raise RuntimeError('image_archive_size_budget_exhausted')
    def budget():
        if time.monotonic()-started>MAX_VALIDATION_SECONDS:raise RuntimeError('image_archive_validation_timeout')
    class HashedReader:
        def __init__(self,stream):self.stream=stream;self.digest=hashlib.sha256();self.count=0
        def read(self,size=-1):
            budget();value=self.stream.read(size);self.digest.update(value);self.count+=len(value);return value
    def member_bytes(bundle,entry,limit):
        if entry.size>limit:raise RuntimeError('image_archive_metadata_too_large')
        stream=bundle.extractfile(entry)
        with stream:return stream.read(limit+1)
    with tarfile.open(archive,mode='r:')as bundle:
        members={}
        for entry in bundle:
            budget();path=PurePosixPath(entry.name)
            if (len(members)>=len(layers)+2 or entry.name in members or not entry.isfile()or entry.size<0
                or '\\'in entry.name or path.is_absolute()or '..'in path.parts or str(path)!=entry.name):
                raise RuntimeError('image_archive_member_invalid')
            members[entry.name]=entry
        if 'manifest.json'not in members:raise RuntimeError('image_archive_manifest_missing')
        records=json.loads(member_bytes(bundle,members['manifest.json'],2097152))
        if not isinstance(records,list)or len(records)!=1:raise RuntimeError('image_archive_manifest_invalid')
        record=records[0]
        if record.get('RepoTags')!=[temporary_tag]:raise RuntimeError('image_archive_tags_mismatch')
        names=record.get('Layers')
        if not isinstance(names,list)or len(names)!=len(layers):raise RuntimeError('image_archive_layers_mismatch')
        config_name=record.get('Config')
        if not isinstance(config_name,str)or config_name not in members or not all(isinstance(name,str)and name in members for name in names):
            raise RuntimeError('image_archive_member_reference_invalid')
        required=set(names)|{'manifest.json',config_name}
        if set(members)!=required:raise RuntimeError('image_archive_members_mismatch')
        if member_bytes(bundle,members[config_name],2097152)!=config_raw:
            raise RuntimeError('image_archive_config_mismatch')
        for descriptor,name,diff_id in zip(layers,names,diff_ids):
            entry=members[name]
            if entry.size!=descriptor['size']:raise RuntimeError('image_archive_layer_size_mismatch')
            raw=HashedReader(bundle.extractfile(entry));digest=hashlib.sha256()
            media=descriptor.get('mediaType')
            if media in GZIP_LAYERS:stream=gzip.GzipFile(fileobj=raw,mode='rb')
            elif media in RAW_LAYERS:stream=raw
            else:raise RuntimeError('image_archive_compression_unsupported')
            try:
                while True:
                    chunk=stream.read(1048576)
                    if not chunk:break
                    expanded+=len(chunk);digest.update(chunk);budget()
                    if expanded>MAX_EXPANDED_BYTES:raise RuntimeError('image_archive_expansion_budget_exhausted')
                while raw.read(1048576):pass
            finally:
                if stream is not raw:stream.close()
                raw.stream.close()
            if raw.count!=descriptor['size']or'sha256:'+raw.digest.hexdigest()!=descriptor['digest']:
                raise RuntimeError('image_archive_compressed_digest_mismatch')
            if 'sha256:'+digest.hexdigest()!=diff_id:raise RuntimeError('image_archive_rootfs_digest_mismatch')
    return {'verified_layer_count':len(layers),'verified_expanded_bytes':expanded,
        'verified_compressed_layer_bytes':compressed}

CONFIG_DEFAULTS={'Hostname':'','Domainname':'','User':'','AttachStdin':False,'AttachStdout':False,
    'AttachStderr':False,'Tty':False,'OpenStdin':False,'StdinOnce':False,'NetworkDisabled':False,
    'ArgsEscaped':False,'Env':[],'Cmd':[],'Entrypoint':[],'OnBuild':[],'Shell':[],
    'Volumes':{},'ExposedPorts':{},'Labels':{},'WorkingDir':'','Image':'','MacAddress':'',
    'StopSignal':'','StopTimeout':0,'Healthcheck':None}

def semantic_config(values):
    result={}
    for key,value in values.items():
        default=CONFIG_DEFAULTS.get(key)
        if key in CONFIG_DEFAULTS and value is None:value=default
        if key=='Healthcheck'and isinstance(value,dict):
            health_defaults={'Test':[],'Interval':0,'Timeout':0,'StartPeriod':0,'StartInterval':0,'Retries':0}
            value={name:(health_defaults[name]if item is None and name in health_defaults else item)for name,item in value.items()}
            value={name:item for name,item in value.items()if name not in health_defaults or item!=health_defaults[name]}
            value=value or None
        if key not in CONFIG_DEFAULTS or value!=default:result[key]=value
    return result

def verify_loaded_image(info,config,revision):
    actual_id=info.get('Id','')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',actual_id):raise RuntimeError('loaded_image_id_invalid')
    expected=semantic_config(config['config']);actual=semantic_config(info.get('Config')or{})
    if actual!=expected:
        raise RuntimeError('loaded_image_config_mismatch')
    if (info.get('Config',{}).get('Labels')or{}).get('org.opencontainers.image.revision')!=revision:
        raise RuntimeError('loaded_image_revision_mismatch')
    if info.get('Os')!=config['os']or info.get('Architecture')!=config['architecture']:
        raise RuntimeError('loaded_image_platform_mismatch')
    rootfs=info.get('RootFS')or{}
    if rootfs.get('Type')!=config['rootfs']['type']or rootfs.get('Layers')!=config['rootfs']['diff_ids']:
        raise RuntimeError('loaded_image_rootfs_mismatch')
    for source,target in(('variant','Variant'),('os.version','OsVersion'),('os.features','OsFeatures')):
        if source in config and config[source]!=info.get(target):raise RuntimeError('loaded_image_platform_metadata_mismatch')
    if type(info.get('Size'))is not int or info['Size']<=0:raise RuntimeError('loaded_image_size_invalid')
    return actual_id

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);parser.add_argument('--digest',required=True)
    parser.add_argument('--image-size',required=True,type=int)
    parser.add_argument('--deploy-path',required=True);args=parser.parse_args()
    if not re.fullmatch(r'ghcr\.io/btcfoxman/muse2api:[a-f0-9]{40}',args.image):raise RuntimeError('unexpected_image_reference')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',args.digest):raise RuntimeError('expected_manifest_digest_required')
    root=Path(args.deploy_path).resolve()
    if root!=Path('/home/btcfoxman/docker/muse2api'):raise RuntimeError('unexpected_deployment_directory')
    root.joinpath('.pull-tools').mkdir(exist_ok=True,mode=0o700)
    proxy=configured_proxy();routes=[None,proxy]if proxy else[None]
    repository=args.image[len('ghcr.io/'):].rsplit(':',1)[0]
    authorization=None;metadata_route=None;metadata_ok=False
    token_url='https://ghcr.io/token?'+urllib.parse.urlencode({'service':'ghcr.io','scope':'repository:'+repository+':pull'})
    for route in routes:
        try:
            token=json.loads(read_bounded(token_url,route,{'Authorization':'Basic '+basic_auth()}))['token']
            authorization={'Authorization':'Bearer '+token,'Accept':ACCEPT}
            raw=read_bounded('https://ghcr.io/v2/'+repository+'/manifests/'+args.digest,route,authorization)
            if 'sha256:'+hashlib.sha256(raw).hexdigest()!=args.digest:raise RuntimeError('registry_manifest_digest_mismatch')
            metadata_route=route;metadata_ok=True;break
        except Exception:continue
    if not metadata_ok:raise RuntimeError('registry_manifest_unavailable')
    manifest=json.loads(raw);image_digest=args.digest
    if 'manifests'in manifest:
        candidates=[row for row in manifest['manifests']if row.get('platform',{}).get('os')=='linux'and row.get('platform',{}).get('architecture')=='amd64']
        if len(candidates)!=1:raise RuntimeError('image_platform_not_unique')
        image_digest=candidates[0]['digest']
        raw=read_bounded('https://ghcr.io/v2/'+repository+'/manifests/'+image_digest,metadata_route,authorization)
        if 'sha256:'+hashlib.sha256(raw).hexdigest()!=image_digest:raise RuntimeError('image_manifest_digest_mismatch')
        manifest=json.loads(raw)
    config_digest=manifest['config']['digest']
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',config_digest):raise RuntimeError('invalid_image_config')
    if type(manifest['config'].get('size'))is not int or not 0<manifest['config']['size']<=2097152:
        raise RuntimeError('invalid_image_config_size')
    if not all(re.fullmatch(r'sha256:[a-f0-9]{64}',str(row.get('digest',''))) for row in manifest['layers']):
        raise RuntimeError('invalid_layer_digest')
    if not all(type(row.get('size'))is int and row['size']>0 for row in manifest['layers']):
        raise RuntimeError('invalid_layer_size')
    revision=args.image.rsplit(':',1)[1]
    config_raw=None
    for route in dict.fromkeys([metadata_route,*routes]):
        try:
            candidate=read_bounded('https://ghcr.io/v2/'+repository+'/blobs/'+config_digest,route,authorization)
            config=validate_config(candidate,manifest['config'],revision)
            config_raw=candidate;break
        except Exception:continue
    if config_raw is None:raise RuntimeError('registry_verified_config_unavailable')
    layers=[row for row in manifest['layers']if row['size']>=1048576]
    if not layers:raise RuntimeError('image_transfer_sample_unavailable')
    layer=max(layers,key=lambda row:row['size'])
    samples=[sample('https://ghcr.io/v2/'+repository+'/blobs/'+layer['digest'],route,authorization)for route in routes]
    ordered=sorted(zip(routes,samples),key=lambda pair:(pair[1]['complete'],pair[1]['bytes_per_second']),reverse=True)
    if not any(item['complete']for _,item in ordered):raise RuntimeError('image_transfer_routes_unavailable')
    compressed=sum(row['size']for row in manifest['layers'])
    check_disk_budget(root,compressed,args.image_size)
    with tempfile.TemporaryDirectory(prefix='verified-pull-',dir=root/'.pull-tools')as temporary:
        folder=Path(temporary);binary=acquire_tool(folder,[route for route,_ in ordered],root/'.pull-tools/cache')
        archive=folder/'image.tar';selected=None
        for route,_ in ordered:
            try:
                pull(binary,'ghcr.io/'+repository+'@'+image_digest,archive,route)
                selected='proxy'if route else'direct';break
            except Exception:archive.unlink(missing_ok=True)
        if selected is None:raise RuntimeError('all_bounded_image_transfer_routes_failed')
        temporary_tag='ghcr.io/'+repository+':i-was-a-digest'
        try:archive_proof=validate_archive(archive,manifest,config_raw,config,temporary_tag)
        except RuntimeError:raise
        except (OSError,ValueError,TypeError,KeyError,tarfile.TarError):
            raise RuntimeError('image_archive_validation_failed')from None
        # Docker stores differ in their reported Size. Budget with the measured,
        # cryptographically checked uncompressed layer bytes before importing.
        check_disk_budget(root,compressed,archive_proof['verified_expanded_bytes'])
        command(['docker','load','-i',str(archive)],timeout=240,stage='docker_load_verified_archive')
        loaded=command(['docker','image','inspect',temporary_tag],stage='docker_inspect_loaded_tag')
        try:
            info=json.loads(loaded)[0]
            actual_id=verify_loaded_image(info,config,revision)
        except RuntimeError:raise
        except (ValueError,TypeError,KeyError,IndexError,AttributeError):
            raise RuntimeError('docker_loaded_metadata_invalid')from None
        command(['docker','tag',actual_id,args.image],stage='docker_tag_verified_revision')
    receipt={'image':args.image,'manifest_digest':args.digest,'platform_manifest_digest':image_digest,
        'loaded_config_digest':config_digest,'loaded_image_id':actual_id,'selected_route':selected,'route_samples':samples,
        'cloud_reported_image_size':args.image_size,'loaded_reported_image_size':info['Size'],
        'store_size_reporting_difference':info['Size']-args.image_size,'archive_proof':archive_proof,
        'crane_archive_sha256':TOOL_SHA,'docker_daemon_proxy_changed':False,'builds_or_dependency_installs':0}
    (root/'last-image-pull.safe.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))

if __name__=='__main__':
    try:main()
    except Exception as error:raise SystemExit('Verified image pull failed: '+str(error)if isinstance(error,RuntimeError)else'Verified image pull failed (details redacted)')
