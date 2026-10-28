// SPDX-License-Identifier: GPL-2.0
/* Experimental runtime-only CANcase XL driver, firmware 8.7.16.
 * USB protocol derived from this project's validated Python implementation.
 * Bus-off restart requires hardware validation; no FD or silent mode yet.
 */
#include <linux/module.h>
#include <linux/usb.h>
#include <linux/kthread.h>
#include <linux/mutex.h>
#include <linux/can/dev.h>
#include <linux/can/skb.h>
#include <linux/can/error.h>
#include <linux/delay.h>
#include <linux/version.h>
#include <linux/ethtool.h>
#include <linux/net_tstamp.h>
#include <linux/uaccess.h>
#include <linux/sockios.h>
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 12, 0)
#include <linux/unaligned.h>
#else
#include <asm/unaligned.h>
#endif

#include "license_decode.h"

#define RX_SIZE 65536
struct xl;
#define DIAG_VARIANTS 32
#define DIAG_STATES 256
struct xl_diag_error {
 u8 key[3], first[24], last[24];
 unsigned int count;
};
struct xl_chan {
 struct can_priv can; /* must be first */
 struct xl *xl;
 struct net_device *net;
 unsigned int index;
 spinlock_t txlock;
 spinlock_t berr_lock; /* netlink snapshot; never take io from the getter */
 struct can_berr_counter bec;
 bool bec_valid;
 struct sk_buff *queued;
 struct can_frame sent;
 bool active, pending;
 bool closing; /* serialized with restart and RX by io */
 bool pending_can_error; /* confirmed TX-side CAN error for the pending frame */
 unsigned long deadline;
 bool diagnostic_armed;
 unsigned long diagnostic_deadline;
 unsigned int diagnostic_states, diagnostic_variants, diagnostic_untracked;
 struct xl_diag_error diagnostic_errors[DIAG_VARIANTS];
 u8 diagnostic_last_state[24];
};
struct xl {
 struct usb_device *usb;
 u32 serial, article, firmware[3];
 u8 license_key[8], license_mask[32];
 bool licenses_valid, oem_valid;
 u8 oem_mask[16];
 u16 piggy[2], transceiver[2], license_updates;
 struct net_device *net[2];
 struct task_struct *thread;
 struct mutex io; /* commands, RX parsing, echo ownership and channel transitions */
 bool dead, fault, ready;
 u8 *cmd, *reply, *rx, *tx; /* heap buffers required for USB DMA */
};
/* A/B comparison: false restores the 0.1.3 idle delay policy. */
static bool fast_poll = true;
/* Managed firmware handoff: one exact USB enumeration may probe once.
 * Existing devices remain bound while another loader owns its USB interface.
 * Manual insmod retains the historical runtime-only workflow by default. */
static bool managed_handoff;
module_param(managed_handoff, bool, 0444);
MODULE_PARM_DESC(managed_handoff, "Require a one-shot bind_target after firmware preparation");
static char bind_target[32] = "none";
module_param_string(bind_target, bind_target, sizeof(bind_target), 0600);
MODULE_PARM_DESC(bind_target, "One-shot USB bus:address allowed to probe (managed handoff only)");

static bool handoff_allowed(struct usb_device *usb)
{
 char target[32];
 bool allowed;
 if (!managed_handoff) return true;
 snprintf(target, sizeof(target), "%u:%u", usb->bus->busnum, usb->devnum);
 kernel_param_lock(THIS_MODULE);
 allowed = !strcmp(target, bind_target);
 if (allowed) strscpy(bind_target, "none", sizeof(bind_target));
 kernel_param_unlock(THIS_MODULE);
 return allowed;
}

module_param(fast_poll, bool, 0444);
MODULE_PARM_DESC(fast_poll, "Avoid idle sleep while TX awaits a hardware receipt (default true)");

/* Restrictive fallback: stop the affected channel on its first CAN error. */
static bool isolate_can_errors;
module_param(isolate_can_errors, bool, 0444);
MODULE_PARM_DESC(isolate_can_errors, "Restrictive mode: stop channel on CAN error; false continues verified CAN states");

/* One selected channel only; observation ends three seconds after first TX/error.
 * Read-only module parameter: never a production bus-off implementation. */
static unsigned int diagnose_channel;
module_param(diagnose_channel, uint, 0444);
MODULE_PARM_DESC(diagnose_channel, "Bounded raw CAN error diagnosis: 0=off, 1/2=channel; 3 seconds maximum");

/* Global hardware receive filters, independent of per-socket CAN_RAW filters.
 * Read-only: changing profiles requires unloading the module with channels down.
 * Full bitmap writes avoid relying on unverified partial-update semantics. */
static unsigned int std_filter[2];
module_param_array(std_filter, uint, NULL, 0444);
MODULE_PARM_DESC(std_filter, "Experimental standard RX profiles ch1,ch2: 0=all, 1=0x123, 2=0x120-0x12f");

static void standard_bitmap(u8 *bitmap, unsigned int profile, bool on)
{
 memset(bitmap, 0, 256);
 if (!on) return;
 if (profile == 0) memset(bitmap, 255, 256);
 else if (profile == 1) bitmap[0x24] = 0x08;
 else if (profile == 2) { bitmap[0x24] = 255; bitmap[0x25] = 255; }
}

static bool diagnostic_channel(struct xl_chan *c)
{
 return diagnose_channel == c->index + 1;
}
static void diagnostic_arm(struct xl_chan *c)
{
 if (!diagnostic_channel(c) || c->diagnostic_armed) return;
 c->diagnostic_armed = true;
 c->diagnostic_deadline = jiffies + 3 * HZ;
}

/* Separate state budget; repeating errors cannot consume it.
 * Raw bytes only: no assumptions about firmware controller-state encoding. */
static void diagnostic_record(struct xl_chan *c, const u8 *r)
{
 unsigned int i;
 struct xl_diag_error *entry;
 u8 key[3] = { r[10], r[14], r[15] };
 if (!diagnostic_channel(c) || !c->active) return;
 if (r[0] == 4) {
  memcpy(c->diagnostic_last_state, r, 24);
  if (c->diagnostic_states < DIAG_STATES)
   dev_info(&c->xl->usb->dev, "diagnostic ch%u state-record=%*ph\n", c->index + 1, 24, r);
  c->diagnostic_states++;
  return;
 }
 if (r[0] != 2) return;
 for (i = 0; i < c->diagnostic_variants; i++)
  if (!memcmp(c->diagnostic_errors[i].key, key, sizeof(key))) break;
 if (i == c->diagnostic_variants) {
  if (i == DIAG_VARIANTS) { c->diagnostic_untracked++; return; }
  entry = &c->diagnostic_errors[i];
  memcpy(entry->key, key, sizeof(key));
  memcpy(entry->first, r, 24);
  c->diagnostic_variants++;
 } else entry = &c->diagnostic_errors[i];
 entry->count++;
 memcpy(entry->last, r, 24);
}
static void diagnostic_summary(struct xl_chan *c)
{
 unsigned int i;
 for (i = 0; i < c->diagnostic_variants; i++) {
  struct xl_diag_error *entry = &c->diagnostic_errors[i];
  dev_info(&c->xl->usb->dev, "diagnostic ch%u error-count=%u first-record=%*ph last-record=%*ph\n",
           c->index + 1, entry->count, 24, entry->first, 24, entry->last);
 }
 if (c->diagnostic_states > DIAG_STATES)
  dev_info(&c->xl->usb->dev, "diagnostic ch%u last-state-record=%*ph\n", c->index + 1, 24, c->diagnostic_last_state);
 dev_info(&c->xl->usb->dev, "diagnostic ch%u summary states=%u state-dropped=%u error-untracked=%u\n",
          c->index + 1, c->diagnostic_states,
          c->diagnostic_states > DIAG_STATES ? c->diagnostic_states - DIAG_STATES : 0,
          c->diagnostic_untracked);
}

static const u32 rates[] = {125000, 250000, 500000, 1000000};

static int command(struct xl *x, unsigned int len, unsigned int expected)
{
 int ret, actual = 0;
 const char *phase = "write";
 u16 op = get_unaligned_le16(x->cmd);
 if (x->dead || x->fault)
  return -ENODEV;
 ret = usb_bulk_msg(x->usb, usb_sndbulkpipe(x->usb, 1), x->cmd, len, &actual, 2000);
 if (!ret && actual != len) ret = -EIO;
 if (!ret) {
  phase = "reply";
  actual = 0;
  ret = usb_bulk_msg(x->usb, usb_rcvbulkpipe(x->usb, 1), x->reply, 512, &actual, 2000);
  if (!ret && (actual != expected || get_unaligned_le16(x->reply) != (op | 0x80) ||
                get_unaligned_le16(x->reply + 2))) ret = -EPROTO;
 }
 if (ret) {
  dev_err(&x->usb->dev,
          "command op=0x%02x field=%u phase=%s error=%d actual=%d expected=%u\n",
          op, len >= 4 ? get_unaligned_le16(x->cmd + 2) : 0,
          phase, ret, actual, phase[0] == 'w' ? len : expected);
  if (phase[0] == 'r' && actual > 0)
   dev_err(&x->usb->dev, "command reply: %*ph\n", min(actual, 32), x->reply);
  x->fault = true; /* never retry a possibly consumed command */
 }
 return ret;
}
static void setup_cmd(struct xl *x, u16 op, u16 ch)
{
 memset(x->cmd, 0, 512);
 put_unaligned_le16(op, x->cmd);
 put_unaligned_le16(ch, x->cmd + 2);
}
static int simple(struct xl *x, u16 op, u16 ch, unsigned int len)
{
 setup_cmd(x, op, ch);
 return command(x, len, 6);
}
static int filter(struct xl *x, unsigned int ch, bool on)
{
 setup_cmd(x, 0x22, ch);
 x->cmd[4] = 1; x->cmd[9] = 1;
 standard_bitmap(x->cmd + 10, std_filter[ch], on);
 return command(x, 267, 6);
}
static int start_hw(struct xl_chan *c)
{
 struct xl *x = c->xl;
 u32 rate = c->can.bittiming.bitrate;
 int ret = simple(x, 0x23, c->index, 12);
 if (ret) return ret;
 ret = filter(x, c->index, true);
 if (ret) return ret;
 setup_cmd(x, 0x1d, c->index);
 put_unaligned_le32(rate, x->cmd + 4);
 put_unaligned_le32(rate == 500000 ? 4 : 5, x->cmd + 8);
 put_unaligned_le32(rate == 500000 ? 3 : 2, x->cmd + 12);
 put_unaligned_le32(rate == 500000 ? 1 : 2, x->cmd + 16);
 put_unaligned_le32(1, x->cmd + 20);
 ret = command(x, 24, 6);
 if (ret) return ret;
 setup_cmd(x, 0x1c, c->index);
 put_unaligned_le32(1, x->cmd + 4);
 ret = command(x, 8, 6);
 if (ret) return ret;
 return simple(x, 0x18, c->index, 4);
}
static int stop_hw(struct xl_chan *c)
{
 struct xl *x = c->xl;
 int ret;
 setup_cmd(x, 0x20, c->index);
 put_unaligned_le32(9, x->cmd + 4);
 ret = command(x, 8, 6);
 if (!ret) ret = simple(x, 0x19, c->index, 4);
 if (!ret) ret = filter(x, c->index, false);
 return ret;
}
static void cancel_tx(struct xl_chan *c)
{
 struct sk_buff *skb;
 spin_lock_bh(&c->txlock);
 skb = c->queued; c->queued = NULL;
 spin_unlock_bh(&c->txlock);
 if (skb) { dev_kfree_skb_any(skb); c->net->stats.tx_dropped++; }
 if (c->pending) {
  can_free_echo_skb(c->net, 0, NULL);
  c->pending = false;
  c->net->stats.tx_dropped++;
 }
}
static void fault_device(struct xl *x, int err)
{
 unsigned int i;
 x->fault = true;
 dev_err(&x->usb->dev, "transport/protocol fault %d; replug required (not classified as bus-off)\n", err);
 for (i = 0; i < 2; i++) {
  struct xl_chan *c = netdev_priv(x->net[i]);
  netif_device_detach(c->net);
  netif_carrier_off(c->net);
  cancel_tx(c);
  c->active = false;
  c->can.state = CAN_STATE_STOPPED;
 }
}
/* A channel-local CAN/status/TX-receipt problem is not a USB failure.
 * Only a failed stop command escalates to the device-wide transport handler.
 */
static int halt_channel(struct xl_chan *c, const char *reason)
{
 int ret;
 netif_tx_disable(c->net);
 netif_carrier_off(c->net);
 ret = stop_hw(c);
 if (ret) return ret;
 c->active = false;
 cancel_tx(c);
 c->pending_can_error = false;
 c->can.state = CAN_STATE_STOPPED;
 dev_warn(&c->xl->usb->dev, "channel %u stopped: %s; down/up required\n",
          c->index + 1, reason);
 return 0;
}

/* Called by worker with io held, independently of further RX records. */
static int diagnostic_tick(struct xl_chan *c)
{
 int ret;
 if (!c->diagnostic_armed || !time_after_eq(jiffies, c->diagnostic_deadline)) return 0;
 netif_tx_disable(c->net);
 netif_carrier_off(c->net);
 ret = stop_hw(c);
 diagnostic_summary(c);
 if (ret) return ret; /* global transport fault handled by worker */
 c->active = false;
 cancel_tx(c);
 c->can.state = CAN_STATE_STOPPED;
 dev_info(&c->xl->usb->dev, "diagnostic ch%u window ended; stopped (not classified as bus-off)\n", c->index + 1);
 return 0;
}

/* Firmware 8.7.16: Windows ASC/USB comparison, 4204 errors, 31 explicit
 * counter pairs and warning/passive status transitions. Tag 2: state/TX/RX
 * at 10/11/12; tag 4: at 11/12/13. Bus-off status 1 verified
 * against a Windows short-circuit capture; its counter bytes are not valid.
 */
static int decode_status(const u8 *r, enum can_state *state, u8 *tx, u8 *rx)
{
 unsigned int off = r[0] == 2 ? 10 : 11;
 switch (r[off]) {
 case 8: *state = CAN_STATE_ERROR_ACTIVE; break;
 case 4: *state = CAN_STATE_ERROR_WARNING; break;
 case 2: *state = CAN_STATE_ERROR_PASSIVE; break;
 case 1: *state = CAN_STATE_BUS_OFF; break;
 default: return -EPROTO; /* stop conservatively; do not guess bus-off */
 }
 *tx = r[off + 1]; *rx = r[off + 2];
 return 0;
}
static void apply_status(struct xl_chan *c, enum can_state state, u8 tx, u8 rx,
                         struct can_frame *cf)
{
 enum can_state tx_state = CAN_STATE_ERROR_ACTIVE, rx_state = CAN_STATE_ERROR_ACTIVE;
 unsigned long flags;
 spin_lock_irqsave(&c->berr_lock, flags);
 c->bec.txerr = tx; c->bec.rxerr = rx; c->bec_valid = true;
 spin_unlock_irqrestore(&c->berr_lock, flags);
 if (cf) {
  cf->can_id |= CAN_ERR_CNT;
  cf->data[6] = tx; cf->data[7] = rx;
 }
 /* Raw status determines overall state. Attribute its direction to the
  * larger counter, as in other SocketCAN USB drivers; equal means both. */
 if (tx >= rx) tx_state = state;
 if (rx >= tx) rx_state = state;
 if (state != c->can.state)
  can_change_state(c->net, cf, tx_state, rx_state);
}
static int xl_get_berr_counter(const struct net_device *net, struct can_berr_counter *bec)
{
 struct xl_chan *c = netdev_priv(net);
 unsigned long flags;
 int ret;
 spin_lock_irqsave(&c->berr_lock, flags);
 ret = c->bec_valid ? 0 : -EOPNOTSUPP;
 if (!ret) *bec = c->bec;
 spin_unlock_irqrestore(&c->berr_lock, flags);
 return ret;
}
/* io held. Stop hardware before publishing bus-off to prevent unsolicited
 * recovery and settle pending TX ownership before CAN core restart work runs.
 * Do not publish the firmware's bus-off counter sentinel as a real counter.
 */
static int enter_bus_off(struct xl_chan *c)
{
 struct can_frame *cf = NULL;
 struct sk_buff *skb;
 unsigned long flags;
 int ret;
 if (c->can.state == CAN_STATE_BUS_OFF || !c->active || c->closing) return 0;
 netif_tx_disable(c->net);
 netif_carrier_off(c->net);
 ret = stop_hw(c);
 if (ret) return ret;
 c->active = false;
 c->diagnostic_armed = false;
 cancel_tx(c);
 c->pending_can_error = false;
 spin_lock_irqsave(&c->berr_lock, flags);
 c->bec_valid = false;
 spin_unlock_irqrestore(&c->berr_lock, flags);
 skb = alloc_can_err_skb(c->net, &cf);
 if (!skb) { cf = NULL; c->net->stats.rx_dropped++; }
 can_change_state(c->net, cf, CAN_STATE_BUS_OFF, CAN_STATE_ERROR_ACTIVE);
 if (skb) {
  c->net->stats.rx_packets++; c->net->stats.rx_bytes += CAN_ERR_DLC;
  netif_rx(skb);
 }
 /* CAN core owns restart-ms timing and manual restart semantics. */
 can_bus_off(c->net);
 return 0;
}

static int controller_status(struct xl_chan *c, const u8 *r)
{
 struct can_frame *cf = NULL;
 struct sk_buff *skb = NULL;
 enum can_state state;
 u8 tx, rx;
 int ret;
 if (!c->active || c->closing) return 0;
 ret = decode_status(r, &state, &tx, &rx);
 if (ret) {
  dev_err(&c->xl->usb->dev, "unverified controller status: %*ph\n", 24, r);
  return halt_channel(c, "unverified controller status (not classified as bus-off)");
 }
 if (state == CAN_STATE_BUS_OFF) return enter_bus_off(c);
 if (state != c->can.state) {
  skb = alloc_can_err_skb(c->net, &cf);
  if (!skb) { cf = NULL; c->net->stats.rx_dropped++; }
 }
 apply_status(c, state, tx, rx, cf);
 if (skb) {
  c->net->stats.rx_packets++; c->net->stats.rx_bytes += CAN_ERR_DLC;
  netif_rx(skb);
 }
 return 0;
}

/* Called with io held. ECC and state/counters verified against CANalyzer. */
static int can_error(struct xl_chan *c, const u8 *r)
{
 struct can_frame *cf = NULL;
 struct sk_buff *skb;
 enum can_state state;
 u8 tx, rx, ecc = r[14];
 int ret;
 if (!c->active || c->closing) return 0; /* late records after a confirmed stop */
 ret = decode_status(r, &state, &tx, &rx);
 if (ret) {
  dev_err(&c->xl->usb->dev, "unverified error status: %*ph\n", 24, r);
  return halt_channel(c, "unverified CAN error status (not classified as bus-off)");
 }
 if (state == CAN_STATE_BUS_OFF) return enter_bus_off(c);
 c->can.can_stats.bus_error++;
 if (ecc & 0x20) c->net->stats.rx_errors++;
 else c->net->stats.tx_errors++;
 skb = alloc_can_err_skb(c->net, &cf);
 if (!skb) cf = NULL;
 apply_status(c, state, tx, rx, cf);
 if (skb) {
  cf->can_id |= CAN_ERR_PROT | CAN_ERR_BUSERROR;
  if (!(ecc & 0x20)) cf->data[2] |= CAN_ERR_PROT_TX;
  switch (ecc >> 6) {
  case 0: cf->data[2] |= CAN_ERR_PROT_BIT; break;
  case 1: cf->data[2] |= CAN_ERR_PROT_FORM; break;
  case 2: cf->data[2] |= CAN_ERR_PROT_STUFF; break;
  }
  /* SJA1000 ECC segment values match SocketCAN CAN_ERR_PROT_LOC_*.
   * Unknown positions remain unspecified instead of publishing raw status.
   */
  switch (ecc & 0x1f) {
  case 0x03: case 0x02: case 0x06: case 0x04: case 0x05:
  case 0x07: case 0x0f: case 0x0e: case 0x0c: case 0x0d:
  case 0x09: case 0x0b: case 0x0a: case 0x08: case 0x18:
  case 0x19: case 0x1b: case 0x1a: case 0x12:
   cf->data[3] = ecc & 0x1f; break;
  }
  if (ecc == 0xd9) cf->can_id |= CAN_ERR_ACK;
  c->net->stats.rx_packets++; c->net->stats.rx_bytes += CAN_ERR_DLC;
  netif_rx(skb);
 } else c->net->stats.rx_dropped++;
 dev_warn_ratelimited(&c->xl->usb->dev,
          "channel %u CAN error: ECC=%02x raw10-15=%*ph; isolate=%u\n",
          c->index + 1, ecc, 6, r + 10, isolate_can_errors);
 if (diagnostic_channel(c)) { diagnostic_arm(c); return 0; }
 if (!isolate_can_errors) {
  /* Do not discard a frame which the controller can still retransmit.
   * Keep one echo slot and backpressure until receipt or explicit down/up.
   * RX-side errors do not explain a missing TX receipt.
   */
  if (c->pending && !(ecc & 0x20)) c->pending_can_error = true;
  return 0;
 }
 return halt_channel(c, "restrictive CAN error policy");
}
/* Firmware event bytes 4..9: unsigned 48-bit little-endian 16 MHz clock.
 * Return raw device time, NOT host wall time. Both channels share this domain.
 * No host anchor, per-channel offset or monotonic clamping: resets and the
 * approximately 203.6-day wrap remain observable. Integer ns truncate 0.5 ns.
 * The maximum 48-bit value times 125 fits comfortably in u64 and ktime_t.
 */
static u64 xl_timestamp_ns(const u8 *r)
{
 u64 ticks = (u64)get_unaligned_le32(r + 4) |
             ((u64)get_unaligned_le16(r + 8) << 32);
 return (ticks * 125) >> 1;
}

static int record(struct xl *x, const u8 *r)
{
 struct xl_chan *c;
 struct can_frame *cf;
 struct sk_buff *skb;
 canid_t id;
 unsigned int ch = r[1] & 0x7f;
 if (ch > 1) {
  dev_err(&x->usb->dev, "RX record rejected: invalid channel=%u tag=0x%02x\n", ch, r[0]);
  return -EPROTO;
 }
 c = netdev_priv(x->net[ch]);
 if (r[1] & 0x80) { c->net->stats.rx_over_errors++; return -EOVERFLOW; }
 diagnostic_record(c, r);
 if (r[0] == 2) return can_error(c, r);
 if (r[0] == 4) return controller_status(c, r);
 if (r[0] != 1) return 0;
 if (r[14] > 8 || (r[15] & ~0x05)) {
  dev_err(&x->usb->dev, "RX record rejected: channel=%u length=%u flags=0x%02x\n",
          ch + 1, r[14], r[15]);
  return -EPROTO;
 }
 id = get_unaligned_le32(r + 10);
 if ((id & ~CAN_EFF_FLAG) > ((id & CAN_EFF_FLAG) ? CAN_EFF_MASK : CAN_SFF_MASK)) {
  dev_err(&x->usb->dev, "RX record rejected: channel=%u invalid CAN ID=0x%08x\n", ch + 1, id);
  return -EPROTO;
 }
 if (r[15] & 0x04) id |= CAN_RTR_FLAG;
 if (!c->active || c->closing) return 0;
 if (r[15] & 0x01) {
  if (!c->pending || c->sent.can_id != id || c->sent.len != r[14] ||
      (!(id & CAN_RTR_FLAG) && memcmp(c->sent.data, r + 16, r[14]))) {
   dev_err(&x->usb->dev,
           "TX receipt rejected: channel=%u pending=%u expected_id=0x%08x received_id=0x%08x expected_len=%u received_len=%u data_match=%u\n",
           ch + 1, c->pending, c->sent.can_id, id, c->sent.len, r[14],
           !!((id & CAN_RTR_FLAG) || !memcmp(c->sent.data, r + 16, r[14])));
   return -EPROTO;
  }
  /* The CAN echo helper delivers requested TX timestamps to MSG_ERRQUEUE.
   * Set the stamp before it consumes the echo skb. io protects ownership.
   */
  skb = c->can.echo_skb[0];
  if (skb) skb_hwtstamps(skb)->hwtstamp = ns_to_ktime(xl_timestamp_ns(r));
  c->net->stats.tx_bytes += can_get_echo_skb(c->net, 0, NULL);
  c->pending = false; c->pending_can_error = false;
  c->net->stats.tx_packets++;
  netif_wake_queue(c->net);
 } else {
  skb = alloc_can_skb(c->net, &cf);
  if (!skb) { c->net->stats.rx_dropped++; return 0; }
  skb_hwtstamps(skb)->hwtstamp = ns_to_ktime(xl_timestamp_ns(r));
  cf->can_id = id; cf->len = r[14];
  if (!(id & CAN_RTR_FLAG)) memcpy(cf->data, r + 16, cf->len);
  c->net->stats.rx_packets++;
  c->net->stats.rx_bytes += (id & CAN_RTR_FLAG) ? 0 : cf->len;
  netif_rx(skb);
 }
 return 0;
}
/* Validate entire transfer layout before processing any of its records. */
static int rx_layout_fault(struct xl *x, unsigned int len, unsigned int off,
                           const char *reason)
{
 dev_err(&x->usb->dev, "RX layout rejected: endpoint=0x82 actual=%u offset=%u reason=%s\n",
         len, off, reason);
 return -EPROTO;
}
static int parse_rx(struct xl *x, unsigned int len)
{
 unsigned int off, n, size, i;
 int ret;
 for (i = 0; i < 2; i++) {
  off = 0;
  while (off < len) {
   if (len - off < 4) return rx_layout_fault(x, len, off, "truncated count");
   n = get_unaligned_le32(x->rx + off);
   if (n > (len - off - 4) / 24) return rx_layout_fault(x, len, off, "count exceeds transfer");
   size = 4 + n * 24;
   if (off == 0 && size == len) { /* compact transfer */ }
   else if (n == 21 && len - off >= 512) {
    if (get_unaligned_le32(x->rx + off + 508)) return rx_layout_fault(x, len, off, "nonzero padding");
    size = 512;
   } else if (n > 21 || size != len - off) return rx_layout_fault(x, len, off, "invalid block size");
   if (i) for (unsigned int j = 0; j < n; j++) {
    ret = record(x, x->rx + off + 4 + j * 24);
    if (ret) {
     dev_err(&x->usb->dev, "RX record failed: endpoint=0x82 actual=%u offset=%u tag=0x%02x error=%d\n",
             len, off + 4 + j * 24, x->rx[off + 4 + j * 24], ret);
     return ret;
    }
   }
   off += size;
  }
 }
 return len >= 4 ? 0 : rx_layout_fault(x, len, 0, "missing count");
}
static int receive(struct xl *x, unsigned int timeout)
{
 int actual = 0, ret, status;
 ret = usb_bulk_msg(x->usb, usb_rcvbulkpipe(x->usb, 2), x->rx, RX_SIZE, &actual, timeout);
 if (ret && ret != -ETIMEDOUT) {
  dev_err(&x->usb->dev, "USB RX failed: endpoint=0x82 status=%d actual=%d requested=%u timeout_ms=%u\n",
          ret, actual, RX_SIZE, timeout);
  return ret;
 }
 status = ret;
 if (actual) {
  ret = parse_rx(x, actual);
  if (ret) {
   dev_err(&x->usb->dev, "RX parsing failed: endpoint=0x82 usb_status=%d actual=%d error=%d\n",
           status, actual, ret);
   return ret;
  }
  /* A valid count=0 packet means no events, not drain progress. */
  return actual == 4 && !get_unaligned_le32(x->rx) ? 0 : 1;
 }
 if (ret == -ETIMEDOUT) return 0;
 dev_err(&x->usb->dev, "USB RX rejected: endpoint=0x82 status=0 actual=0 reason=empty successful transfer\n");
 return -EPROTO;
}
static int drain(struct xl *x)
{
 unsigned long until = jiffies + msecs_to_jiffies(500);
 unsigned int transfers = 0;
 int ret;
 do {
  ret = receive(x, 1);
  if (ret <= 0) {
   if (ret) dev_err(&x->usb->dev, "drain receive failed: %d after %u transfers\n", ret, transfers);
   return ret;
  }
  transfers++;
  /* Both channels share this endpoint. An active peer may produce events
   * indefinitely, especially ACK errors while this channel is stopped.
   * Process (never discard) a bounded batch, then let the channel transition
   * proceed. The worker resumes parsing the remaining stream under io.
   * With no active channel, retain the strict idle/deadline check.
   */
  if (transfers >= 16 &&
      (((struct xl_chan *)netdev_priv(x->net[0]))->active ||
       ((struct xl_chan *)netdev_priv(x->net[1]))->active))
   return 0;
 } while (time_before(jiffies, until));
 dev_err(&x->usb->dev, "drain deadline exceeded: %u transfers, last count=%u\n",
         transfers, get_unaligned_le32(x->rx));
 return -ETIMEDOUT;
}
static int worker(void *arg)
{
 struct xl *x = arg;
 while (!kthread_should_stop()) {
  unsigned int i;
  int ret = 0;
  bool idle = false, tx_busy = false;
  mutex_lock(&x->io);
  if (x->dead || x->fault) { mutex_unlock(&x->io); break; }
  if (!x->ready) { mutex_unlock(&x->io); msleep(10); continue; }
  for (i = 0; i < 2 && !ret; i++) {
   struct xl_chan *c = netdev_priv(x->net[i]);
   struct sk_buff *skb;
   int actual = 0;
   if (!c->active || c->closing) continue;
   ret = diagnostic_tick(c);
   if (ret || !c->active) continue;
   if (c->pending) {
    if (time_after_eq(jiffies, c->deadline) &&
        (isolate_can_errors || !c->pending_can_error))
     ret = halt_channel(c, "TX receipt timeout");
    continue;
   }
   spin_lock_bh(&c->txlock);
   skb = c->queued; c->queued = NULL;
   spin_unlock_bh(&c->txlock);
   if (!skb) continue;
   c->sent = *(struct can_frame *)skb->data;
   memset(x->tx, 0, 24); x->tx[0] = 1; x->tx[1] = i;
   put_unaligned_le16(0x106, x->tx + 2);
   put_unaligned_le32(c->sent.can_id & ~CAN_RTR_FLAG, x->tx + 10);
   x->tx[14] = c->sent.len;
   x->tx[15] = (c->sent.can_id & CAN_RTR_FLAG) ? 0x05 : 0x01;
   if (!(c->sent.can_id & CAN_RTR_FLAG))
    memcpy(x->tx + 16, c->sent.data, c->sent.len);
   ret = can_put_echo_skb(skb, c->net, 0, 0);
   if (ret) { c->net->stats.tx_dropped++; break; }
   diagnostic_arm(c);
   c->pending_can_error = false;
   c->pending = true; c->deadline = jiffies + 5 * HZ;
   ret = usb_bulk_msg(x->usb, usb_sndbulkpipe(x->usb, 3 + i), x->tx, 24, &actual, 2000);
   if (ret || actual != 24)
    dev_err(&x->usb->dev, "USB TX failed: channel=%u endpoint=0x%02x status=%d actual=%d expected=24\n",
            i + 1, 3 + i, ret, actual);
   if (!ret && actual != 24) ret = -EIO;
  }
  if (!ret) { ret = receive(x, 1); idle = ret == 0; if (ret > 0) ret = 0; }
  if (ret) fault_device(x, ret);
  /* count=0 does not imply an idle device: a receipt can still be pending.
   * Keep polling in that case, and while a newly queued skb awaits USB.
   * Retain the original sleep when there is no TX work (including RX-only).
   * All pending state is protected by io; queued is protected by txlock. */
  if (fast_poll && !ret) {
   for (i = 0; i < 2; i++) {
    struct xl_chan *c = netdev_priv(x->net[i]);
    if (!c->active || c->closing) continue;
    spin_lock_bh(&c->txlock);
    tx_busy |= (c->pending && !c->pending_can_error) || c->queued != NULL;
    spin_unlock_bh(&c->txlock);
   }
  }
  mutex_unlock(&x->io);
  if (idle && !tx_busy) usleep_range(500, 1000);
  else cond_resched();
 }
 return 0;
}
static int xl_open(struct net_device *net)
{
 struct xl_chan *c = netdev_priv(net);
 struct xl *x = c->xl;
 int ret = open_candev(net);
 if (ret) return ret;
 mutex_lock(&x->io);
 if (x->dead || x->fault) ret = -ENODEV;
 else {
  dev_info(&x->usb->dev, "channel %u open: draining RX\n", c->index + 1);
  ret = drain(x);
  if (!ret) {
   dev_info(&x->usb->dev, "channel %u open: configuring %u bit/s\n",
            c->index + 1, c->can.bittiming.bitrate);
   ret = start_hw(c);
  }
  if (ret) fault_device(x, ret);
 }
 if (!ret) {
  c->closing = false;
  spin_lock_irq(&c->berr_lock);
  c->bec_valid = false;
  spin_unlock_irq(&c->berr_lock);
  c->diagnostic_armed = false;
  c->diagnostic_states = 0; c->diagnostic_variants = 0; c->diagnostic_untracked = 0;
  memset(c->diagnostic_errors, 0, sizeof(c->diagnostic_errors));
  if (diagnostic_channel(c))
   dev_info(&x->usb->dev, "diagnostic ch%u begin\n", c->index + 1);
  c->pending_can_error = false;
  c->active = true; c->can.state = CAN_STATE_ERROR_ACTIVE;
  netif_carrier_on(net); netif_start_queue(net);
 }
 mutex_unlock(&x->io);
 if (ret) close_candev(net);
 return ret;
}
/* Called by CAN core for both manual and delayed bus-off restart.
 * Never cancel restart_work here: this callback may be that work itself.
 */
static int xl_set_mode(struct net_device *net, enum can_mode mode)
{
 struct xl_chan *c = netdev_priv(net);
 struct xl *x = c->xl;
 int ret;
 if (mode != CAN_MODE_START) return -EOPNOTSUPP;
 mutex_lock(&x->io);
 if (x->dead || x->fault) { ret = -ENODEV; goto out; }
 if (c->closing || !netif_running(net) || c->can.state != CAN_STATE_BUS_OFF) {
  ret = -EBUSY; goto out;
 }
 /* enter_bus_off already confirmed stop and released the echo slot. */
 ret = drain(x);
 if (!ret) ret = start_hw(c);
 if (ret) { fault_device(x, ret); goto out; }
 spin_lock_irq(&c->berr_lock);
 c->bec_valid = false;
 spin_unlock_irq(&c->berr_lock);
 c->pending_can_error = false;
 c->diagnostic_armed = false;
 c->active = true;
 c->can.state = CAN_STATE_ERROR_ACTIVE;
 netif_wake_queue(net);
 dev_info(&x->usb->dev, "channel %u restarted after bus-off\n", c->index + 1);
out:
 mutex_unlock(&x->io);
 return ret;
}

static int xl_stop(struct net_device *net)
{
 struct xl_chan *c = netdev_priv(net);
 struct xl *x = c->xl;
 int ret = 0;
 /* Block new restart/notification work before waiting, without holding io. */
 mutex_lock(&x->io);
 c->closing = true;
 mutex_unlock(&x->io);
 cancel_delayed_work_sync(&c->can.restart_work);
 netif_tx_disable(net);
 mutex_lock(&x->io);
 if (!x->dead && !x->fault && c->active) ret = stop_hw(c);
 c->active = false;
 cancel_tx(c);
 if (!ret && !x->dead && !x->fault) ret = drain(x);
 if (ret) fault_device(x, ret);
 c->can.state = CAN_STATE_STOPPED;
 netif_carrier_off(net);
 mutex_unlock(&x->io);
 close_candev(net);
 return 0;
}
static netdev_tx_t xl_xmit(struct sk_buff *skb, struct net_device *net)
{
 struct xl_chan *c = netdev_priv(net);
 if (can_dropped_invalid_skb(net, skb)) return NETDEV_TX_OK;
 spin_lock(&c->txlock);
 if (READ_ONCE(c->xl->dead) || READ_ONCE(c->xl->fault)) {
  spin_unlock(&c->txlock); dev_kfree_skb_any(skb); net->stats.tx_dropped++;
  return NETDEV_TX_OK;
 }
 if (c->queued) { spin_unlock(&c->txlock); return NETDEV_TX_BUSY; }
 netif_stop_queue(net);
 c->queued = skb;
 spin_unlock(&c->txlock);
 return NETDEV_TX_OK;
}
/* This device supports Classical CAN only. No CAN-FD/XL MTU transition. */
static int xl_change_mtu(struct net_device *net, int mtu)
{
 if (mtu != CAN_MTU)
  return -EINVAL;
 net->mtu = CAN_MTU;
 return 0;
}
/* Hardware stamps are always present. Match CAN's fixed ON/ALL policy. */
static int xl_timestamp_ioctl(struct net_device *net, struct ifreq *ifr, int cmd)
{
 struct hwtstamp_config cfg = { .tx_type = HWTSTAMP_TX_ON,
                               .rx_filter = HWTSTAMP_FILTER_ALL };
 (void)net;
 if (cmd == SIOCSHWTSTAMP) {
  if (copy_from_user(&cfg, ifr->ifr_data, sizeof(cfg))) return -EFAULT;
  if (cfg.flags) return -EINVAL;
  if (cfg.tx_type != HWTSTAMP_TX_ON || cfg.rx_filter != HWTSTAMP_FILTER_ALL)
   return -ERANGE;
 } else if (cmd != SIOCGHWTSTAMP) return -EOPNOTSUPP;
 return copy_to_user(ifr->ifr_data, &cfg, sizeof(cfg)) ? -EFAULT : 0;
}
static int xl_get_ts_info(struct net_device *net,
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 11, 0)
                          struct kernel_ethtool_ts_info *info)
#else
                          struct ethtool_ts_info *info)
#endif
{
 (void)net;
 info->so_timestamping = SOF_TIMESTAMPING_SOFTWARE |
  SOF_TIMESTAMPING_TX_SOFTWARE | SOF_TIMESTAMPING_RX_SOFTWARE |
  SOF_TIMESTAMPING_TX_HARDWARE | SOF_TIMESTAMPING_RX_HARDWARE |
  SOF_TIMESTAMPING_RAW_HARDWARE;
 info->phc_index = -1; /* No PTP clock or host-clock synchronization. */
 info->tx_types = BIT(HWTSTAMP_TX_ON);
 info->rx_filters = BIT(HWTSTAMP_FILTER_ALL);
 return 0;
}
static const struct ethtool_ops xl_ethtool_ops = {
 .get_ts_info = xl_get_ts_info,
};
static const struct net_device_ops xl_ops = {
 .ndo_open = xl_open, .ndo_stop = xl_stop, .ndo_start_xmit = xl_xmit,
 .ndo_change_mtu = xl_change_mtu,
 .ndo_eth_ioctl = xl_timestamp_ioctl,
};
/* Read metadata once, before worker/netdev registration. Queries never start CAN. */
static int read_identity(struct xl *x)
{
 int ret;
 setup_cmd(x, 0x25, 0); x->cmd[5] = 1; x->cmd[23] = 0xff;
 ret = command(x, 24, 24); if (ret) return ret;
 setup_cmd(x, 0x10, 0); put_unaligned_le32(1, x->cmd + 4);
 put_unaligned_le32(0x4200, x->cmd + 8); put_unaligned_le32(8, x->cmd + 12);
 ret = command(x, 18, 25); if (ret) return ret;
 if (memcmp(x->reply + 4, x->cmd + 4, 12)) return -EPROTO;
 put_unaligned_le32(0x4208, x->cmd + 8); put_unaligned_le32(0xb0, x->cmd + 12);
 ret = command(x, 18, 193); if (ret) return ret;
 if (memcmp(x->reply + 4, x->cmd + 4, 12) ||
     memcmp(x->reply + 56, "Vector", 6) || memcmp(x->reply + 88, "CANcaseXL", 9)) return -EPROTO;
 /* Identity data offset 24: Windows xlGetLicenseUpdateCounter (u16 LE). */
 x->license_updates = get_unaligned_le16(x->reply + 40);
 memcpy(x->license_key, x->reply + 166, 8);
 x->serial = get_unaligned_le32(x->reply + 152);
 x->article = get_unaligned_le32(x->reply + 174);
 if (!x->serial || x->serial == U32_MAX) return -EPROTO;
 return 0;
}
/* Same fixed read sequence as Windows; executed only before netdev registration. */
static int read_licenses(struct xl *x)
{
 struct xl_lic_cipher *cipher;
 int ret;
 setup_cmd(x, 0x25, 0); x->cmd[5] = 1; x->cmd[23] = 0xff;
 ret = command(x, 24, 24); if (ret) goto out;
 setup_cmd(x, 0x10, 0); put_unaligned_le32(1, x->cmd + 4);
 put_unaligned_le32(0x6000, x->cmd + 8); put_unaligned_le32(8, x->cmd + 12);
 ret = command(x, 18, 25); if (ret) goto out;
 if (memcmp(x->reply + 4, x->cmd + 4, 12)) { ret = -EPROTO; goto out; }
 put_unaligned_le32(46, x->cmd + 12);
 ret = command(x, 18, 63); if (ret) goto out;
 if (memcmp(x->reply + 4, x->cmd + 4, 12)) { ret = -EPROTO; goto out; }
 cipher = kmalloc(sizeof(*cipher), GFP_KERNEL);
 if (!cipher) { ret = -ENOMEM; goto out; }
 x->licenses_valid = !xl_license_decode(cipher, x->license_key, x->reply + 16, x->license_mask);
 memzero_explicit(cipher, sizeof(*cipher)); kfree(cipher);
 if (!x->licenses_valid) dev_warn(&x->usb->dev, "License record unsupported or checksum invalid; license information unavailable\n");
 /* Invalid license contents never gate CAN operation; transport errors still abort. */
 ret = 0;
out:
 memzero_explicit(x->license_key, sizeof(x->license_key));
 return ret;
}
static int read_oem_licenses(struct xl *x)
{
 struct xl_lic_cipher *cipher;
 int ret;
 setup_cmd(x, 0x25, 0); x->cmd[5] = 1; x->cmd[23] = 0xff;
 ret = command(x, 24, 24); if (ret) return ret;
 setup_cmd(x, 0x10, 0); put_unaligned_le32(1, x->cmd + 4);
 put_unaligned_le32(0x6064, x->cmd + 8); put_unaligned_le32(8, x->cmd + 12);
 ret = command(x, 18, 25); if (ret) return ret;
 if (memcmp(x->reply + 4, x->cmd + 4, 12)) return -EPROTO;
 put_unaligned_le32(30, x->cmd + 12);
 ret = command(x, 18, 47); if (ret) return ret;
 if (memcmp(x->reply + 4, x->cmd + 4, 12)) return -EPROTO;
 cipher = kmalloc(sizeof(*cipher), GFP_KERNEL);
 if (!cipher) return -ENOMEM;
 x->oem_valid = !xl_oem_decode(cipher, x->serial, x->reply + 16, x->oem_mask);
 memzero_explicit(cipher, sizeof(*cipher)); kfree(cipher);
 if (!x->oem_valid) dev_warn(&x->usb->dev, "OEM license record unsupported or checksum invalid\n");
 return 0;
}
static int read_piggy(struct xl *x, unsigned int channel)
{
 unsigned long deadline = jiffies + msecs_to_jiffies(2000);
 int ret = simple(x, 4, channel, 6);
 if (ret) return ret;
 do {
  unsigned int off = 0;
  int actual = 0;
  bool found = false;
  ret = usb_bulk_msg(x->usb, usb_rcvbulkpipe(x->usb, 2), x->rx, RX_SIZE, &actual, 100);
  if (ret == -ETIMEDOUT) continue;
  if (ret) return ret;
  if (actual < 0 || actual > RX_SIZE) return -EPROTO;
  while (off < (unsigned int)actual) {
   unsigned int n, used, span, j;
   if (actual - off < 4) return -EPROTO;
   n = get_unaligned_le32(x->rx + off);
   if (n > 21) return -EPROTO;
   used = 4 + 24 * n;
   span = actual - off >= 512 ? 512 : used;
   if (used > span || span > actual - off) return -EPROTO;
   for (j = 0; j < n; j++) {
    const u8 *r = x->rx + off + 4 + 24*j;
    if (r[0] == 5 && (r[1] & 0x7f) == channel) {
     if (r[1] & 0x80) return -EOVERFLOW;
     x->piggy[channel] = get_unaligned_le16(r + 18);
     x->transceiver[channel] = get_unaligned_le16(r + 20);
     found = true;
    }
   }
   for (j = used; j < span; j++) if (x->rx[off + j]) return -EPROTO;
   off += span;
  }
  if (found) return 0;
 } while (time_before(jiffies, deadline));
 return -ETIMEDOUT;
}
/* All attributes are immutable probe-time snapshots; show() performs no USB I/O. */
static ssize_t serial_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 return sysfs_emit(buf, "%06u\n", c->xl->serial);
}
static DEVICE_ATTR_RO(serial);
static ssize_t article_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 return sysfs_emit(buf, "%06u\n", c->xl->article);
}
static DEVICE_ATTR_RO(article);
static ssize_t channel_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 return sysfs_emit(buf, "%u\n", c->index + 1);
}
static DEVICE_ATTR_RO(channel);
static ssize_t firmware_version_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 return sysfs_emit(buf, "%u.%u.%u\n", c->xl->firmware[0], c->xl->firmware[1], c->xl->firmware[2]);
}
static DEVICE_ATTR_RO(firmware_version);
static ssize_t piggy_type_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 return sysfs_emit(buf, "0x%04x\n", c->xl->piggy[c->index]);
}
static DEVICE_ATTR_RO(piggy_type);
static ssize_t piggy_name_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 const char *name = "unknown";
 switch (c->xl->piggy[c->index]) {
 case 0x101: name = "CANpiggy 251 (Highspeed)"; break;
 case 0x135: name = "CANpiggy 251mag (Highspeed)"; break;
 case 0x136: name = "CANpiggy 1050mag (Highspeed)"; break;
 }
 return sysfs_emit(buf, "%s\n", name);
}
static DEVICE_ATTR_RO(piggy_name);
static ssize_t transceiver_state_initial_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 return sysfs_emit(buf, "0x%04x\n", c->xl->transceiver[c->index]);
}
static DEVICE_ATTR_RO(transceiver_state_initial);
static ssize_t licenses_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 unsigned int i; int len = 0;
 if (!c->xl->licenses_valid) return sysfs_emit(buf, "unavailable\n");
 for (i = 0; i < 256; i++)
  if (c->xl->license_mask[i / 8] & BIT(i % 8))
   len += sysfs_emit_at(buf, len, "%s0x%03X", len ? " " : "", i + 1);
 return len + sysfs_emit_at(buf, len, "\n");
}
static DEVICE_ATTR_RO(licenses);
static ssize_t oem_codes_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 unsigned int base, i; int len = 0;
 if (!c->xl->oem_valid) return sysfs_emit(buf, "unavailable\n");
 for (base = 0; base < 16; base += 8) {
  if (base) len += sysfs_emit_at(buf, len, " ");
  for (i = 8; i > 0; i--) len += sysfs_emit_at(buf, len, "%02x", c->xl->oem_mask[base+i-1]);
 }
 return len + sysfs_emit_at(buf, len, "\n");
}
static DEVICE_ATTR_RO(oem_codes);
static ssize_t license_updates_show(struct device *dev, struct device_attribute *attr, char *buf)
{
 struct xl_chan *c = netdev_priv(to_net_dev(dev));
 return sysfs_emit(buf, "%u\n", (unsigned int)c->xl->license_updates);
}
static DEVICE_ATTR_RO(license_updates);
static struct attribute *xl_info_attrs[] = {
 &dev_attr_serial.attr, &dev_attr_article.attr, &dev_attr_channel.attr,
 &dev_attr_firmware_version.attr, &dev_attr_piggy_type.attr, &dev_attr_piggy_name.attr,
 &dev_attr_transceiver_state_initial.attr, &dev_attr_licenses.attr, &dev_attr_oem_codes.attr, &dev_attr_license_updates.attr, NULL
};
static const struct attribute_group xl_info_group = {
 .name = "cancasexl", .attrs = xl_info_attrs,
};

static int init_runtime(struct xl *x)
{
 unsigned int i;
 int ret = simple(x, 0x33, 0, 2);
 if (!ret) ret = simple(x, 0x2a, 0, 8);
 if (ret) return ret;
 setup_cmd(x, 0x24, 0);
 ret = command(x, 24, 24);
 if (ret) return ret;
 if (get_unaligned_le32(x->reply + 4) != 8 || get_unaligned_le32(x->reply + 8) != 7 ||
     get_unaligned_le32(x->reply + 12) != 16) return -EPROTO;
 for (i = 0; i < 3; i++) x->firmware[i] = get_unaligned_le32(x->reply + 4 + 4*i);
 for (i = 0; i < 2; i++) { ret = read_piggy(x, i); if (ret) return ret; }
 ret = read_identity(x); if (ret) return ret;
 ret = read_licenses(x); if (ret) return ret;
 ret = read_oem_licenses(x); if (ret) return ret;
 ret = simple(x, 5, 1, 4);
 if (!ret) ret = simple(x, 0x31, 0, 8);
 if (ret) return ret;
 setup_cmd(x, 0x2a, 0); put_unaligned_le32(250, x->cmd + 4);
 ret = command(x, 8, 6);
 for (i = 0; i < 2 && !ret; i++) {
  ret = simple(x, 0x19, i, 4);
  if (!ret) ret = simple(x, 0x23, i, 12);
  if (!ret) ret = filter(x, i, false);
 }
 return ret;
}
static void release(struct xl *x)
{
 kfree(x->cmd); kfree(x->reply); kfree(x->rx); kfree(x->tx);
 usb_put_dev(x->usb); kfree(x);
}
static int xl_probe(struct usb_interface *intf, const struct usb_device_id *id)
{
 struct usb_device *usb = interface_to_usbdev(intf);
 struct xl *x;
 unsigned int i, registered = 0, endpoints = 0;
 int ret;
 if (std_filter[0] > 2 || std_filter[1] > 2) return -EINVAL;
 if (intf->cur_altsetting->desc.bInterfaceNumber != 0 ||
     usb->actconfig->desc.bConfigurationValue != 2) {
  dev_info(&intf->dev, "runtime config 2 required; prepare firmware before loading module\n");
  return -ENODEV;
 }
 for (i = 0; i < intf->cur_altsetting->desc.bNumEndpoints; i++) {
  struct usb_endpoint_descriptor *e = &intf->cur_altsetting->endpoint[i].desc;
  if (!usb_endpoint_xfer_bulk(e) || usb_endpoint_maxp(e) != 512) return -ENODEV;
  switch (e->bEndpointAddress) {
  case 1: endpoints |= 1; break; case 0x81: endpoints |= 2; break;
  case 0x82: endpoints |= 4; break; case 3: endpoints |= 8; break;
  case 4: endpoints |= 16; break; default: return -ENODEV;
  }
 }
 if (endpoints != 31) return -ENODEV;
 if (!handoff_allowed(usb)) return -ENODEV;
 x = kzalloc(sizeof(*x), GFP_KERNEL);
 if (!x) return -ENOMEM;
 x->usb = usb_get_dev(usb); mutex_init(&x->io);
 x->cmd = kmalloc(512, GFP_KERNEL); x->reply = kmalloc(512, GFP_KERNEL);
 x->rx = kmalloc(RX_SIZE, GFP_KERNEL); x->tx = kmalloc(24, GFP_KERNEL);
 ret = -ENOMEM;
 if (!x->cmd || !x->reply || !x->rx || !x->tx) goto fail;
 ret = init_runtime(x);
 if (ret) goto fail;
 for (i = 0; i < 2; i++) {
  struct net_device *net = alloc_candev(sizeof(struct xl_chan), 1);
  struct xl_chan *c;
  if (!net) { ret = -ENOMEM; goto fail; }
  x->net[i] = net; c = netdev_priv(net);
  c->xl = x; c->net = net; c->index = i; spin_lock_init(&c->txlock);
  spin_lock_init(&c->berr_lock);
  c->can.do_get_berr_counter = xl_get_berr_counter;
  c->can.do_set_mode = xl_set_mode;
  c->can.state = CAN_STATE_STOPPED;
  c->can.bitrate_const = rates; c->can.bitrate_const_cnt = ARRAY_SIZE(rates);
  net->ethtool_ops = &xl_ethtool_ops;
  net->netdev_ops = &xl_ops; net->flags |= IFF_ECHO;
  /* Largest uint32 serial produces a 15-character name, within IFNAMSIZ. */
  snprintf(net->name, IFNAMSIZ, "can%06u_%u", x->serial, i + 1);
  net->sysfs_groups[0] = &xl_info_group;
  SET_NETDEV_DEV(net, &intf->dev);
 }
 /* Start worker before exposing devices; both are initially inactive. */
 x->thread = kthread_run(worker, x, "cancasexl-usb");
 if (IS_ERR(x->thread)) { ret = PTR_ERR(x->thread); x->thread = NULL; goto fail; }
 for (i = 0; i < 2; i++) {
  ret = register_candev(x->net[i]);
  if (ret) goto fail;
  registered++;
 }
 mutex_lock(&x->io); x->ready = true; mutex_unlock(&x->io);
 usb_set_intfdata(intf, x);
 dev_info(&intf->dev, "experimental runtime driver: %s ch1, %s ch2\n", x->net[0]->name, x->net[1]->name);
 return 0;
fail:
 if (x->thread) kthread_stop(x->thread);
 while (registered) unregister_candev(x->net[--registered]);
 for (i = 0; i < 2; i++) if (x->net[i]) free_candev(x->net[i]);
 release(x); return ret;
}
static void xl_disconnect(struct usb_interface *intf)
{
 struct xl *x = usb_get_intfdata(intf);
 unsigned int i;
 usb_set_intfdata(intf, NULL);
 mutex_lock(&x->io);
 /* Manual unbind/module unload must stop physical CAN before dropping USB.
  * Physical unplug may already have changed the USB state or faulted I/O. */
 if (x->usb->state != USB_STATE_NOTATTACHED && !x->fault) {
  for (i = 0; i < 2; i++) {
   struct xl_chan *c = netdev_priv(x->net[i]);
   if (c->active && stop_hw(c)) {
    dev_warn(&x->usb->dev, "hardware stop failed; unplug device\n");
    break;
   }
  }
 }
 x->dead = true;
 mutex_unlock(&x->io);
 kthread_stop(x->thread);
 for (i = 0; i < 2; i++) { unregister_candev(x->net[i]); free_candev(x->net[i]); }
 release(x);
}
static const struct usb_device_id xl_ids[] = {
 { USB_DEVICE_INTERFACE_NUMBER(0x1248, 0x1001, 0) }, { }
};
MODULE_DEVICE_TABLE(usb, xl_ids);
static struct usb_driver xl_driver = {
 .name = "cancasexl", .probe = xl_probe, .disconnect = xl_disconnect, .id_table = xl_ids,
};
module_usb_driver(xl_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Experimental CANcase XL runtime-only SocketCAN driver");
MODULE_VERSION("0.1.20");

