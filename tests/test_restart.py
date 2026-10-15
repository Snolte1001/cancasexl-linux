"""Exercise the actual restart callback with synthetic USB/lifecycle outcomes."""
from pathlib import Path
import subprocess
import tempfile
source = (Path(__file__).resolve().parents[1] / 'kernel/cancasexl.c').read_text()
callback = source[source.index('static int xl_set_mode('):source.index('static int xl_stop(')]
prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <linux/can/netlink.h>
enum can_mode { CAN_MODE_STOP, CAN_MODE_START };
struct xl { bool dead, fault; int io; };
struct xl_chan { struct xl *xl; bool closing, bec_valid, pending_can_error,
 diagnostic_armed, active; int berr_lock; unsigned index;
 struct { enum can_state state; } can; };
struct net_device { struct xl_chan *priv; bool running; };
#define netdev_priv(n) ((n)->priv)
#define netif_running(n) ((n)->running)
#define mutex_lock(x) ((void)(x))
#define mutex_unlock(x) ((void)(x))
#define spin_lock_irq(x) ((void)(x))
#define spin_unlock_irq(x) ((void)(x))
#define dev_info(...) ((void)0)
static int drains, starts, wakes, faults, drain_result, start_result;
static int drain(struct xl *x) { (void)x; drains++; return drain_result; }
static int start_hw(struct xl_chan *c) { (void)c; starts++; return start_result; }
static void fault_device(struct xl *x,int r) { (void)r; faults++; x->fault=true; }
static void netif_wake_queue(struct net_device *n) { (void)n; wakes++; }
'''
checks = r'''
int main(void) {
 struct xl x={0}; struct xl_chan c={.xl=&x};
 struct net_device n={.priv=&c,.running=true};
 c.can.state=CAN_STATE_BUS_OFF;
 assert(xl_set_mode(&n,CAN_MODE_STOP)==-EOPNOTSUPP && drains==0);
 c.closing=true;
 assert(xl_set_mode(&n,CAN_MODE_START)==-EBUSY && drains==0);
 c.closing=false; n.running=false;
 assert(xl_set_mode(&n,CAN_MODE_START)==-EBUSY && drains==0);
 n.running=true; x.dead=true;
 assert(xl_set_mode(&n,CAN_MODE_START)==-ENODEV && drains==0);
 x.dead=false; x.fault=true;
 assert(xl_set_mode(&n,CAN_MODE_START)==-ENODEV && drains==0);
 x.fault=false;
 assert(xl_set_mode(&n,CAN_MODE_START)==0);
 assert(c.active && c.can.state==CAN_STATE_ERROR_ACTIVE && wakes==1);
 assert(drains==1 && starts==1 && faults==0 && !c.bec_valid);
 assert(xl_set_mode(&n,CAN_MODE_START)==-EBUSY && starts==1);
 c.active=false; c.can.state=CAN_STATE_BUS_OFF; drain_result=-EIO;
 assert(xl_set_mode(&n,CAN_MODE_START)==-EIO && starts==1 && faults==1);
 assert(!c.active && wakes==1);
 x.fault=false; drain_result=0; start_result=-ETIMEDOUT;
 assert(xl_set_mode(&n,CAN_MODE_START)==-ETIMEDOUT && faults==2);
 assert(!c.active && wakes==1);
 return 0;
}
'''
with tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp)
    (p/'test.c').write_text(prefix+callback+checks)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)
print('PASS: restart callback, shutdown/disconnect guards and USB failure propagation')
