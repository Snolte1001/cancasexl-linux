"""Compile the actual receive-record handler with synthetic kernel stubs."""
import pathlib, subprocess, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
source = (ROOT / "kernel/cancasexl.c").read_text()
handler = source[source.index("static int record("):source.index("/* Validate entire transfer")]
stub = r"""
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <assert.h>
#include <errno.h>
#include <stdio.h>
typedef uint8_t u8;
typedef uint32_t canid_t;
#define CAN_MAX_RAW_DLC 15
#define CAN_EFF_FLAG 0x80000000U
#define CAN_RTR_FLAG 0x40000000U
#define CAN_EFF_MASK 0x1fffffffU
#define CAN_SFF_MASK 0x7ffU
#define CAN_CTRLMODE_CC_LEN8_DLC 0x100
#define can_cc_dlc2len(d) ((d)>8?8:(d))
struct can_frame { canid_t can_id; u8 len, pad[2], len8_dlc, data[8]; };
static u8 can_get_cc_dlc(const struct can_frame *f, unsigned m) {
 return (m & CAN_CTRLMODE_CC_LEN8_DLC) && f->len==8 && f->len8_dlc>8 && f->len8_dlc<=15 ? f->len8_dlc : f->len;
}
static void can_frame_set_cc_len(struct can_frame *f,u8 d,unsigned m) {
 if ((m & CAN_CTRLMODE_CC_LEN8_DLC) && d>8) f->len8_dlc=d;
 f->len=can_cc_dlc2len(d);
}
struct sk_buff { struct { long hwtstamp; } stamp; };
struct net_device { struct { unsigned rx_over_errors,tx_bytes,tx_packets,rx_dropped,rx_packets,rx_bytes; } stats; void *priv; };
struct xl_chan { struct net_device *net; struct { struct sk_buff *echo_skb[1]; unsigned ctrlmode; } can; bool active,closing,pending,pending_can_error; struct can_frame sent; };
struct usb_device { int dev; };
struct xl { struct usb_device *usb; struct net_device *net[2]; };
#define netdev_priv(n) ((struct xl_chan *)(n)->priv)
#define dev_err(dev, ...) ((void)(dev))
#define diagnostic_record(c,r) ((void)(c),(void)(r))
#define can_error(c,r) (0)
#define controller_status(c,r) (0)
#define skb_hwtstamps(s) (&(s)->stamp)
#define ns_to_ktime(t) (t)
#define xl_timestamp_ns(r) (0L)
#define netif_wake_queue(n) ((void)(n))
#define netif_rx(s) ((void)(s))
static struct { struct can_frame frame; unsigned guard; } output;
static struct sk_buff skb;
static struct sk_buff *alloc_can_skb(struct net_device *n, struct can_frame **f) {
 (void)n; memset(&output,0,sizeof(output)); output.guard=0x12345678; *f=&output.frame; return &skb;
}
static unsigned can_get_echo_skb(struct net_device *n,int i,void *p) {
 (void)i;(void)p;struct xl_chan *c=n->priv;return c->sent.can_id & CAN_RTR_FLAG ? 0 : c->sent.len;
}
static unsigned get_unaligned_le32(const u8 *p) {
 return p[0]|p[1]<<8|p[2]<<16|(unsigned)p[3]<<24;
}
"""
cases = r"""
int main(void) {
 struct usb_device usb={0};
 struct net_device nets[2]={0};
 struct xl_chan chans[2]={0};
 struct xl x={.usb=&usb,.net={&nets[0],&nets[1]}};
 unsigned tests=0;
 for(unsigned ch=0;ch<2;ch++) {
  nets[ch].priv=&chans[ch];chans[ch].net=&nets[ch];chans[ch].active=true;
  for(unsigned mode=0;mode<2;mode++) for(unsigned ext=0;ext<2;ext++)
  for(unsigned rtr=0;rtr<2;rtr++) for(unsigned dlc=0;dlc<16;dlc++) {
   u8 r[24]={1, ch}; unsigned id=ext?CAN_EFF_FLAG|0x1234567:0x621;
   struct xl_chan *c=&chans[ch];c->can.ctrlmode=mode?CAN_CTRLMODE_CC_LEN8_DLC:0;
   memcpy(r+10,&id,4);r[14]=dlc;r[15]=rtr?4:0;
   memset(r+16,0x5a,8);
   unsigned len=dlc>8?8:dlc, before=nets[ch].stats.rx_bytes;
   assert(record(&x,r)==0);assert(output.frame.len==len);
   assert(output.frame.len8_dlc==((mode && dlc>8)?dlc:0));
   assert(output.guard==0x12345678);
   assert(nets[ch].stats.rx_bytes-before==(rtr?0:len));
   for(unsigned i=0;i<8;i++) assert(output.frame.data[i]==(!rtr && i<len?0x5a:0));
   /* Echo the DLC that the TX helper would actually put on the wire. */
   c->sent=output.frame;c->pending=true;r[15]|=1;
   r[14]=can_get_cc_dlc(&c->sent,c->can.ctrlmode);
   assert(record(&x,r)==0);assert(!c->pending);
   c->pending=true;r[14]=(r[14]+1)%16;
   assert(record(&x,r)==-EPROTO);assert(c->pending);
   tests++;
  }
 }
 for(unsigned dlc=16;dlc<256;dlc++) {
  u8 r[24]={1,0};r[14]=dlc;assert(record(&x,r)==-EPROTO);
 }
 printf("PASS: %u RX/echo combinations; all DLC values above 15 rejected\n",tests);
}
"""
with tempfile.TemporaryDirectory() as tmp:
    c=pathlib.Path(tmp)/"record.c"; exe=pathlib.Path(tmp)/"record"
    c.write_text(stub+handler+cases)
    subprocess.run(["cc","-std=gnu11","-Wall","-Wextra","-Werror","-fsanitize=undefined","-fno-sanitize-recover=all",str(c),"-o",str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
