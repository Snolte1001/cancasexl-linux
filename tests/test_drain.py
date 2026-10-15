"""Exercise the actual RX drain loop with synthetic continuous peer traffic."""
from pathlib import Path
import subprocess
import tempfile
source = (Path(__file__).resolve().parents[1] / 'kernel/cancasexl.c').read_text()
function = source[source.index('static int drain('):source.index('static int worker(')]
prefix = r'''
#include <stdbool.h>
#include <stdint.h>
#include <assert.h>
#include <errno.h>
struct xl_chan { bool active; };
struct xl { struct xl_chan *net[2]; };
#define netdev_priv(n) (n)
#define dev_err(...) ((void)0)
#define msecs_to_jiffies(n) (n)
#define time_before(a,b) ((a)<(b))
static unsigned long jiffies;
static unsigned calls;
static int result=1;
static int receive(struct xl *x, unsigned timeout) {
 (void)x; (void)timeout; calls++; jiffies++; return result;
}
'''
checks = r'''
int main(void) {
 struct xl_chan a={0}, b={.active=true}; struct xl x={.net={&a,&b}};
 assert(drain(&x)==0 && calls==16); /* active peer, continuous stream */
 a.active=true; b.active=false; calls=0;
 assert(drain(&x)==0 && calls==16); /* symmetric channel operation */
 a.active=false; calls=0;
 assert(drain(&x)==-ETIMEDOUT && calls==500); /* no peer: strict drain */
 b.active=true; calls=0; result=-EPROTO;
 assert(drain(&x)==-EPROTO && calls==1); /* real parse failure is preserved */
 calls=0; result=0; assert(drain(&x)==0 && calls==1);
 return 0;
}
'''
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp)
    (p/'test.c').write_text(prefix + function + checks)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)
print('PASS: bounded active-peer drain, strict inactive drain and error propagation')
