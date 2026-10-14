"""Bounded bitrate-mismatch observation. Never declares bus-off from raw bytes."""
import argparse
import json
from pathlib import Path
import socket
import struct
import subprocess
import time
from .kernel_test import ip, snapshot, validate_vector, validate_peer, RATES, check_state
from .diagnostic_capture import ErrorSummary, kernel_capture
from .kernel_errors import require, error_socket, decode_error, isolated


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel',type=int,choices=(1,2),default=1)
    parser.add_argument('--interfaces',nargs=2,default=['canxlk0','canxlk1'])
    parser.add_argument('--peers',nargs=2,required=True)
    parser.add_argument('--report',default='kernel_diagnose.json')
    a=parser.parse_args(argv)
    names=a.interfaces+a.peers
    if len(set(names))!=4 or any(not n or '/' in n or n.startswith('-') for n in names):
        parser.error('four distinct interface names required')
    target=a.channel-1;vector=a.interfaces[target];peer=a.peers[target]
    r=dict(mode='bounded-error-observation',channel=a.channel,started=time.time(),
           observation_completed=False,hardware_busoff_verified=False,error_frames=[],state_samples=[],cleanup_errors=[])
    try: output=open(a.report,'x',encoding='utf-8')
    except OSError as exc:
        print(f'Report cannot be created: {exc}');return 1
    touched=False;peer_changed=False
    captured=ErrorSummary()
    try:
        require(Path('/sys/module/cancasexl/parameters/diagnose_channel').read_text().strip()==str(a.channel),
                'load module with matching diagnose_channel first')
        r['module_version']=Path('/sys/module/cancasexl/version').read_text().strip()
        require(r['module_version'] in ('0.1.8', '0.1.9'),'diagnosis requires module 0.1.8 or 0.1.9')
        r['before']={n:snapshot(n) for n in names}
        for i in (0,1):
            validate_vector(a.interfaces[i])
            require('UP' not in r['before'][a.interfaces[i]]['flags'],'Vector channels must initially be DOWN')
            validate_peer([r['before'][a.peers[i]]],a.peers[i],RATES[i])
        r['kernel_log_before']=subprocess.check_output(['dmesg','--color=never'],text=True,timeout=10).splitlines()[-30:]
        touched=True
        ip('link','set','dev',vector,'type','can','bitrate',str(RATES[target]))
        ip('link','set','dev',vector,'up')
        check_state(snapshot(vector),True,RATES[target])
        # Other Vector channel stays DOWN. Only selected peer is changed.
        peer_changed=True
        ip('link','set','dev',peer,'down')
        ip('link','set','dev',peer,'type','can','bitrate','250000','listen-only','off','restart-ms','0')
        ip('link','set','dev',peer,'up')
        print('Observing mismatched bitrate for up to 3 seconds; no bus-off classification.',flush=True)
        with error_socket(vector) as errors:
            with socket.socket(socket.PF_CAN,socket.SOCK_RAW,socket.CAN_RAW) as tx:
                tx.settimeout(1);tx.bind((vector,))
                require(tx.send(struct.pack('=IB3x8s',0x631,8,b'\x55'*8))==16,'short CAN send')
            deadline=time.monotonic()+4.5
            next_status=0
            drain_until=None
            errors.settimeout(0.02)
            while time.monotonic()<deadline:
                try:
                    event=decode_error(errors.recv(72))
                    captured.add(event,time.time())
                except socket.timeout:pass
                now=time.monotonic()
                if drain_until is not None:
                    if now>=drain_until:break
                elif now>=next_status:
                    next_status=now+0.05
                    sample=snapshot(vector)
                    info=sample['linkinfo']['info_data']
                    if not r['state_samples'] or r['state_samples'][-1]['info_data']!=info:
                        r['state_samples'].append(dict(observed_at=time.time(),info_data=info))
                    if isolated(sample):drain_until=now+0.2
        r['after_observation']={n:snapshot(n) for n in names}
        require(isolated(r['after_observation'][vector]),'diagnostic channel did not stop within observation deadline')
        require(captured.total>0,'no error frames observed')
        r['kernel_error_count']=r['after_observation'][vector]['linkinfo']['info_xstats']['bus_error']-r['before'][vector]['linkinfo']['info_xstats']['bus_error']
        r['socket_count_matches_kernel']=captured.bus_errors==r['kernel_error_count']
        r['observation_completed']=True
    except (Exception,KeyboardInterrupt) as exc:
        r['error']=getattr(exc,'output',None) or str(exc) or 'interrupted'
        print('Diagnosis:',r['error'],flush=True)
    finally:
        if touched:
            try:ip('link','set','dev',vector,'down')
            except Exception as exc:r['cleanup_errors'].append(f'{vector}: {exc}')
        if peer_changed:
            try:
                ip('link','set','dev',peer,'down')
                ip('link','set','dev',peer,'type','can','bitrate',str(RATES[target]),'listen-only','off','restart-ms','0')
                ip('link','set','dev',peer,'up')
                validate_peer([snapshot(peer)],peer,RATES[target])
            except Exception as exc:r['cleanup_errors'].append(f'restore {peer}: {exc}')
        if touched:
            try:
                r['after_cleanup']={n:snapshot(n) for n in names}
                require('UP' not in r['after_cleanup'][vector]['flags'],'target remains UP')
                lines=subprocess.check_output(['dmesg','--color=never'],text=True,timeout=10).splitlines()
                r['kernel_capture']=kernel_capture(lines,a.channel)
            except Exception as exc:r['cleanup_errors'].append(str(exc))
        r.update(captured.report())
        r['capture_complete']=r.get('socket_count_matches_kernel',False) and r.get('kernel_capture',{}).get('complete',False) and captured.untracked==0
        r['finished']=time.time()
        with output:json.dump(r,output,indent=2);output.write('\n')
    ok=r['observation_completed'] and not r['cleanup_errors']
    print(('OBSERVATION COMPLETE' if r['capture_complete'] else 'OBSERVATION COMPLETE; CAPTURE INCOMPLETE')+' (bus-off unverified)' if ok else 'OBSERVATION FAILED', 'Report:',a.report)
    return 0 if ok else 1


if __name__=='__main__':raise SystemExit(main())
