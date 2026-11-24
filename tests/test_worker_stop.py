#!/usr/bin/env python3
"""Compile the actual worker thread against a synthetic kthread stub.

After a transport fault (or a dead device) the worker must not return on its
own: xl_disconnect() always calls kthread_stop(), and stopping a kthread that
already exited is a use-after-free (NULL dereference in kthread_stop).
"""
from pathlib import Path
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / 'kernel/cancasexl.c').read_text()
function = source[source.index('static int worker('):source.index('static int xl_open(')]
prefix = r'''
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <assert.h>
#include <errno.h>
#define HZ 100
#define CAN_RTR_FLAG 0x40000000U
#define TASK_RUNNING 0
#define TASK_INTERRUPTIBLE 1
struct can_frame { uint32_t can_id; uint8_t len; uint8_t data[8]; };
struct sk_buff { unsigned char *data; };
struct net_stats { unsigned long tx_dropped; };
struct net_device { struct net_stats stats; };
struct can_priv { unsigned int ctrlmode; };
struct xl_chan {
 bool active, closing, pending, pending_can_error;
 unsigned long deadline; int txlock;
 struct sk_buff *queued; struct can_frame sent;
 struct net_device *net; struct can_priv can;
};
struct xl {
 int io; bool dead, fault, ready;
 struct xl_chan *net[2]; unsigned char tx[24]; void *usb;
};
static unsigned long jiffies;
static bool fast_poll = true, isolate_can_errors;
static bool stop_requested;
static int locked, schedules, state = TASK_RUNNING, stop_after = 3, polls;
static bool kthread_should_stop(void) { return stop_requested; }
static __attribute__((unused)) void set_current_state(int s) { state = s; }
static __attribute__((unused)) void __set_current_state(int s) { state = s; }
static __attribute__((unused)) void schedule(void) {
 assert(state == TASK_INTERRUPTIBLE && locked == 0);
 if (++schedules >= stop_after) stop_requested = true; /* kthread_stop() wakes the thread */
}
static void mutex_lock(int *m) { assert(*m == 0); *m = 1; locked++; }
static void mutex_unlock(int *m) { assert(*m == 1); *m = 0; locked--; }
static void msleep(unsigned int ms) { (void)ms; if (++polls > 1000) stop_requested = true; }
static void usleep_range(unsigned long a, unsigned long b) { (void)a; (void)b; }
static void cond_resched(void) {}
#define netdev_priv(n) (n)
#define spin_lock_bh(l) ((void)(l))
#define spin_unlock_bh(l) ((void)(l))
#define time_after_eq(a, b) ((long)((a) - (b)) >= 0)
#define dev_err(...) ((void)0)
#define usb_sndbulkpipe(u, e) (e)
#define put_unaligned_le16(v, p) ((void)(v), (void)(p))
#define put_unaligned_le32(v, p) ((void)(v), (void)(p))
#define can_get_cc_dlc(f, m) ((f)->len)
static int diagnostic_tick(struct xl_chan *c) { (void)c; assert(!"no traffic after a fault"); return 0; }
static int halt_channel(struct xl_chan *c, const char *why) { (void)c; (void)why; return 0; }
static void diagnostic_arm(struct xl_chan *c) { (void)c; }
static int can_put_echo_skb(struct sk_buff *s, struct net_device *n, int i, int l) {
 (void)s; (void)n; (void)i; (void)l; return 0;
}
static int usb_bulk_msg(void *u, int p, void *d, int l, int *a, int t) {
 (void)u; (void)p; (void)d; (void)t; *a = l; return 0;
}
static int receive(struct xl *x, unsigned int t) { (void)x; (void)t; assert(!"no USB after a fault"); return 0; }
static void fault_device(struct xl *x, int err) { (void)err; x->fault = true; }
'''
checks = r'''
static void reset(void) { stop_requested = false; schedules = 0; polls = 0; state = TASK_RUNNING; }
int main(void) {
 struct net_device n0 = {{0}}, n1 = {{0}};
 struct xl_chan c0 = { .net = &n0 }, c1 = { .net = &n1 };
 struct xl x = { .ready = true, .net = { &c0, &c1 } };
 x.fault = true; reset();
 assert(worker(&x) == 0);
 assert(schedules == stop_after);      /* waited for kthread_stop(), did not exit early */
 assert(state == TASK_RUNNING && locked == 0);
 x.fault = false; x.dead = true; reset();
 assert(worker(&x) == 0 && schedules == stop_after && state == TASK_RUNNING && locked == 0);
 x.dead = false; x.ready = false; reset(); /* not ready: polls until stopped, no sleep in schedule() */
 assert(worker(&x) == 0 && schedules == 0 && polls > 1000 && locked == 0);
 return 0;
}
'''
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp)
    (p / 'test.c').write_text(prefix + function + checks)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(p / 'test.c'), '-o', str(p / 'test')],
                   check=True)
    subprocess.run([str(p / 'test')], check=True)
print('PASS: worker waits for kthread_stop after a transport fault or a dead device')
