#!/usr/bin/env python3
"""Compile the actual RX parser/transport functions against a synthetic USB stub."""
import pathlib, subprocess, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
source = (ROOT / "kernel/cancasexl.c").read_text()
start = source.index("static int rx_layout_fault")
end = source.index("static int drain", start)
functions = source[start:end]
harness = r"""
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <assert.h>
#define RX_SIZE 65536
struct usb_device { int dev; };
struct xl { struct usb_device *usb; unsigned char *rx; };
static char logs[8192];
#define dev_err(dev, ...) do { (void)(dev); snprintf(logs + strlen(logs), sizeof(logs) - strlen(logs), __VA_ARGS__); } while (0)
static unsigned int get_unaligned_le32(const void *p) {
 const unsigned char *b=p;
 return b[0] | b[1]<<8 | b[2]<<16 | (unsigned int)b[3]<<24;
}
static int records, record_error, usb_status, usb_actual;
static int record(struct xl *x, const unsigned char *r) { (void)x; (void)r; records++; return record_error; }
#define usb_rcvbulkpipe(usb, ep) (ep)
static int usb_bulk_msg(struct usb_device *usb, int ep, void *buf, int size, int *actual, unsigned int timeout) {
 (void)usb; (void)ep; (void)buf; (void)size; (void)timeout;
 *actual=usb_actual; return usb_status;
}
"""
cases = r"""
static unsigned char data[RX_SIZE];
static struct usb_device device;
static struct xl x = { &device, data };
static void setup(int status, int actual) {
 memset(data,0,sizeof(data)); logs[0]=0; records=0; record_error=0;
 usb_status=status; usb_actual=actual;
}
int main(void) {
 setup(-EPROTO,0); assert(receive(&x,1)==-EPROTO); assert(strstr(logs,"USB RX failed"));
 setup(-ETIMEDOUT,0); assert(receive(&x,1)==0); assert(!logs[0]);
 setup(0,0); assert(receive(&x,1)==-EPROTO); assert(strstr(logs,"empty successful transfer"));
 setup(0,4); assert(receive(&x,1)==0); assert(!logs[0]);
 setup(0,28); data[0]=1; assert(receive(&x,1)==1); assert(records==1); assert(!logs[0]);
 setup(-ETIMEDOUT,28); data[0]=1; assert(receive(&x,1)==1); assert(records==1);
 setup(-ETIMEDOUT,3); assert(receive(&x,1)==-EPROTO); assert(strstr(logs,"usb_status=-110"));
 setup(0,3); assert(receive(&x,1)==-EPROTO); assert(strstr(logs,"truncated count"));
 setup(0,4); data[0]=1; assert(receive(&x,1)==-EPROTO); assert(strstr(logs,"count exceeds transfer"));
 setup(0,5); assert(receive(&x,1)==-EPROTO); assert(strstr(logs,"invalid block size"));
 setup(0,516); data[0]=21; data[508]=1; assert(receive(&x,1)==-EPROTO); assert(strstr(logs,"nonzero padding")); assert(!records);
 setup(0,516); data[0]=21; assert(receive(&x,1)==1); assert(records==21);
 setup(0,28); data[0]=1; record_error=-EPROTO; assert(receive(&x,1)==-EPROTO); assert(strstr(logs,"RX record failed"));
 puts("PASS: 13 synthetic RX transport/parser cases");
}
"""
with tempfile.TemporaryDirectory() as temp:
    c = pathlib.Path(temp) / "rx.c"
    exe = pathlib.Path(temp) / "rx"
    c.write_text(harness + functions + cases)
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", str(c), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
