/* SPDX-License-Identifier: GPL-2.0-only
 * CANcase XL RAM firmware preparation, matching native.py. No CAN start/TX.
 * Linux usbfs, OpenSSL SHA-256. No Python or libusb runtime dependency.
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>
#include <openssl/evp.h>
#include <time.h>
#include <dirent.h>
#include <ctype.h>
#include <limits.h>
#ifndef CANCASEXL_SYSFS_ROOT
#define CANCASEXL_SYSFS_ROOT "/sys/bus/usb/devices"
#endif
static unsigned selected_bus, selected_address;

#define OFFSET 293728
#define LENGTH 145738
static int fd=-1, claimed;
static unsigned char reply[512];
static void die(const char *s) { fprintf(stderr,"CANcase XL: %s (%s); no automatic retry\n",s,strerror(errno)); if(claimed) { unsigned n=0; ioctl(fd,USBDEVFS_RELEASEINTERFACE,&n); } if(fd>=0)close(fd); exit(1); }
static unsigned le16(const unsigned char *p){return p[0]|p[1]<<8;}
static uint32_t le32(const unsigned char *p){return (uint32_t)le16(p)|((uint32_t)le16(p+2)<<16);}
static void put32(unsigned char *p,uint32_t v){for(int i=0;i<4;i++)p[i]=(unsigned char)(v>>(8*i));}
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static void hash(const unsigned char *p,size_t n,const char *expected){unsigned char d[32];unsigned len=0;char hex[65];if(!EVP_Digest(p,n,d,&len,EVP_sha256(),NULL)||len!=32)die("SHA256 failed");for(int i=0;i<32;i++)sprintf(hex+2*i,"%02x",d[i]);if(strcmp(hex,expected))die("unverified SYS/firmware hash");}
/* Return exactly the verified RAM image, for either supported input format. */
static unsigned char *firmware(const char *path)
{
 FILE *f = fopen(path, "rb");
 long n;
 unsigned char *p;
 if (!f) die("open firmware input");
 if (fseek(f, 0, SEEK_END) || (n = ftell(f)) <= 0 || n > 16*1024*1024)
  die("firmware input size");
 rewind(f);
 p = malloc((size_t)n);
 if (!p || fread(p, 1, (size_t)n, f) != (size_t)n) die("read firmware input");
 if (fclose(f)) die("close firmware input");
 if (n != LENGTH) {
  if (n < OFFSET + LENGTH) die("SYS size");
  hash(p, (size_t)n, "492e96dbee1578cf3344cf6c62956f63354430eea6b30063ec53ce1b0362b0bc");
  memmove(p, p + OFFSET, LENGTH);
 }
 hash(p, LENGTH, "52f448ab740bd1f328cfcd4cafbc142315b938a0ab3ccd968b06f8660a5aba9b");
 return p;
}
static void extract_firmware(const char *input, const char *output)
{
 unsigned char *p = firmware(input);
 /* No overwrite or symlink following for an existing output. */
 int out = open(output, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
 if (out < 0) die("create firmware output");
 size_t done = 0;
 while (done < LENGTH) {
  ssize_t n = write(out, p + done, LENGTH - done);
  if (n < 0 && errno == EINTR) continue;
  if (n <= 0) die("write firmware output");
  done += (size_t)n;
 }
 if (close(out)) die("close firmware output");
 free(p);
 puts("Verified RAM firmware extracted.");
}
/* sysfs is consulted only when GET_CONFIGURATION stalls. Match the same
 * bus/address and VID/PID; never infer configuration from descriptor count. */
static int sysfs_number(const char *device,const char *attr,int base,int empty_zero)
{
 char path[4096],buf[64],*end;
 if(snprintf(path,sizeof(path),"%s/%s",device,attr)>=(int)sizeof(path)) return -1;
 FILE *f=fopen(path,"r"); if(!f) return -1;
 size_t n=fread(buf,1,sizeof(buf)-1,f);
 int failed=ferror(f); fclose(f);
 if(failed || n==sizeof(buf)-1) return -1;
 buf[n]=0; char *start=buf; while(isspace((unsigned char)*start))start++;
 if(!*start)return empty_zero?0:-1;
 errno=0; long v=strtol(start,&end,base);
 if(errno || end==start || v<0 || v>INT_MAX)return -1;
 while(isspace((unsigned char)*end))end++;
 return *end?-1:(int)v;
}
static unsigned kernel_configuration(void)
{
 DIR *dir=opendir(CANCASEXL_SYSFS_ROOT);
 if(!dir)die("GET_CONFIGURATION stalled; open USB sysfs");
 struct dirent *entry; unsigned matches=0; int config=-1;
 while((entry=readdir(dir))){
  if(entry->d_name[0]=='.' || strchr(entry->d_name,':'))continue;
  char path[4096];
  if(snprintf(path,sizeof(path),"%s/%s",CANCASEXL_SYSFS_ROOT,entry->d_name)>=(int)sizeof(path))continue;
  if(sysfs_number(path,"busnum",10,0)!=(int)selected_bus ||
     sysfs_number(path,"devnum",10,0)!=(int)selected_address)continue;
  matches++;
  if(sysfs_number(path,"idVendor",16,0)!=0x1248 ||
     sysfs_number(path,"idProduct",16,0)!=0x1001)break;
  config=sysfs_number(path,"bConfigurationValue",10,1);
  /* Reject disappearance/address changes during the read. */
  if(sysfs_number(path,"busnum",10,0)!=(int)selected_bus ||
     sysfs_number(path,"devnum",10,0)!=(int)selected_address)config=-1;
 }
 closedir(dir);
 if(matches!=1 || config<0 || config>2){errno=EPROTO;die("GET_CONFIGURATION stalled; sysfs identity/configuration invalid");}
 fprintf(stderr,"CANcase XL: GET_CONFIGURATION stalled; verified sysfs configuration=%d (bus=%u address=%u)\n",config,selected_bus,selected_address);
 return (unsigned)config;
}
static int control(unsigned type,unsigned request,unsigned value,void *data,unsigned length){struct usbdevfs_ctrltransfer t={.bRequestType=type,.bRequest=request,.wValue=value,.wIndex=0,.wLength=length,.timeout=2000,.data=data};int n=ioctl(fd,USBDEVFS_CONTROL,&t);if(n<0 && errno==EPIPE && type==0x80 && request==8 && value==0 && length==1){*(unsigned char *)data=(unsigned char)kernel_configuration();return 1;}if(n!=(int)length){int saved=n<0?errno:EPROTO;fprintf(stderr,"USB control failed: type=0x%02x request=0x%02x value=0x%04x index=0 length=%u returned=%d errno=%d\n",type,request,value,length,n,saved);errno=saved;die("USB control transfer");}return n;}
static int bulk(unsigned ep,void *data,unsigned len,unsigned timeout){struct usbdevfs_bulktransfer t={.ep=ep,.len=len,.timeout=timeout,.data=data};return ioctl(fd,USBDEVFS_BULK,&t);}
static void command(unsigned char *p,unsigned len,unsigned expected){int n;if(bulk(1,p,len,2000)!=(int)len)die("short/failed command write");n=bulk(0x81,reply,sizeof(reply),2000);if(n!=(int)expected||le16(reply)!=(le16(p)|0x80)||le16(reply+2)){fprintf(stderr,"opcode 0x%02x reply length %d\n",p[0],n);die("command reply");}}
/* Configuration 2 can survive resume while RAM firmware is lost. Only a
 * positively identified bootcode response permits switching to boot download.
 * USB errors / unknown replies remain fatal, with no command retries. */
static int runtime_present(void)
{
 unsigned char p[2] = {0x33, 0};
 int n;
 if (bulk(1, p, sizeof(p), 2000) != sizeof(p)) die("runtime probe write");
 n = bulk(0x81, reply, sizeof(reply), 2000);
 if (n == 6 && le16(reply) == 0xb3 && le16(reply+2) == 0) return 1;
 if (n >= 13 && le16(reply) == 0xb3 && le16(reply+2) == 0x40 &&
     !memcmp(reply+4, "bootcode:", 9)) {
  fprintf(stderr, "CANcase XL: bootloader in configuration 2; verifying boot identity before RAM download\n");
  return 0;
 }
 die("runtime probe: unknown/failed reply");
 return 0;
}
static void simple(unsigned op,unsigned channel,unsigned len){unsigned char p[32]={0};p[0]=(unsigned char)op;p[2]=(unsigned char)channel;command(p,len,6);}
/* Read and validate the active configuration descriptor before claiming. */
static void descriptors(unsigned config){unsigned char device[18],head[9],buf[4096];int found=0;control(0x80,6,0x100,device,18);if(le16(device+8)!=0x1248||le16(device+10)!=0x1001)die("wrong USB identity");for(unsigned i=0;i<device[17];i++){control(0x80,6,0x200+i,head,9);unsigned total=le16(head+2);if(total<9||total>sizeof(buf))die("configuration size");if(head[5]!=config)continue;control(0x80,6,0x200+i,buf,total);unsigned mask=0,count=0;int selected=0;for(unsigned pos=0;pos<total;){unsigned len=buf[pos];if(len<2||pos+len>total)die("descriptor framing");if(buf[pos+1]==4){if(len<9)die("interface descriptor");selected=buf[pos+2]==0&&buf[pos+3]==0;}if(selected&&buf[pos+1]==5){if(len<7||(buf[pos+3]&3)!=2||le16(buf+pos+4)!=512)die("endpoint descriptor");switch(buf[pos+2]){case 1:mask|=1;break;case 0x81:mask|=2;break;case 0x82:mask|=4;break;case 3:mask|=8;break;case 4:mask|=16;break;default:die("unknown endpoint");}count++;}pos+=len;}if(mask!=(config==1?3u:31u)||count!=(config==1?2u:5u))die("endpoint set");found=1;}if(!found)die("configuration absent");}
/* Diagnostic only: never modify cached USB descriptors or retry the request. */
static void explain_configuration_failure(unsigned requested, int saved_errno)
{
 if (requested != 2 || saved_errno != EINVAL) return;
 unsigned char device[18] = {0};
 struct usbdevfs_ctrltransfer t = {
  .bRequestType=0x80, .bRequest=6, .wValue=0x100,
  .wLength=sizeof(device), .timeout=2000, .data=device
 };
 if (ioctl(fd, USBDEVFS_CONTROL, &t) != (int)sizeof(device) ||
     device[0]!=18 || device[1]!=1 || le16(device+8)!=0x1248 ||
     le16(device+10)!=0x1001 || device[17]!=2) return;
 DIR *dir=opendir(CANCASEXL_SYSFS_ROOT);
 if (!dir) return;
 unsigned matches=0;
 int cached=-1;
 struct dirent *entry;
 while ((entry=readdir(dir))) {
  if (entry->d_name[0]=='.' || strchr(entry->d_name, ':')) continue;
  char path[4096];
  if (snprintf(path,sizeof(path),"%s/%s",CANCASEXL_SYSFS_ROOT,entry->d_name)>=(int)sizeof(path)) continue;
  if (sysfs_number(path,"busnum",10,0)!=(int)selected_bus ||
      sysfs_number(path,"devnum",10,0)!=(int)selected_address) continue;
  matches++;
  if (sysfs_number(path,"idVendor",16,0)!=0x1248 ||
      sysfs_number(path,"idProduct",16,0)!=0x1001) { cached=-1; break; }
  cached=sysfs_number(path,"bNumConfigurations",10,0);
  if (sysfs_number(path,"busnum",10,0)!=(int)selected_bus ||
      sysfs_number(path,"devnum",10,0)!=(int)selected_address) cached=-1;
 }
 closedir(dir);
 if (matches==1 && cached==1)
  fprintf(stderr,
   "CANcase XL: USB configuration descriptor mismatch (bus=%u address=%u): "
   "device reports 2 configurations, Linux cached 1; runtime configuration 2 "
   "cannot be selected. This firmware transition is currently unsupported. "
   "No automatic reset or retry was performed.\n",selected_bus,selected_address);
}
static void select_config(unsigned config){unsigned n=0;unsigned char current;struct usbdevfs_getdriver driver={.interface=0};if(claimed){if(ioctl(fd,USBDEVFS_RELEASEINTERFACE,&n))die("release interface");claimed=0;}control(0x80,8,0,&current,1);if(current&&ioctl(fd,USBDEVFS_GETDRIVER,&driver)==0)die("kernel driver owns USB interface");if(current!=config&&ioctl(fd,USBDEVFS_SETCONFIGURATION,&config)){int saved=errno;explain_configuration_failure(config,saved);errno=saved;die("set configuration");}if(config){descriptors(config);if(ioctl(fd,USBDEVFS_CLAIMINTERFACE,&n))die("claim interface");claimed=1;}}
/* Only resume permits one recovery of a timed-out, read-only boot query.
 * No download has started. Reset the claimed Vector device, then revalidate. */
static void boot_identity(int resume)
{
 unsigned char boot[60]={0x41};
 if (!resume) { command(boot,60,60); return; }
 if (bulk(1,boot,60,2000)!=60) die("boot query write");
 int n=bulk(0x81,reply,sizeof(reply),2000);
 if(n==60 && le16(reply)==0xc1 && !le16(reply+2)) return;
 if(n>=0 || errno!=ETIMEDOUT) die("boot query: unexpected reply");
 unsigned interface=0;
 if(ioctl(fd,USBDEVFS_RELEASEINTERFACE,&interface)) die("release before resume reset");
 claimed=0;
 struct usbdevfs_getdriver driver={.interface=0};
 if(ioctl(fd,USBDEVFS_GETDRIVER,&driver)==0 || errno!=ENODATA)
  die("resume reset: USB ownership uncertain");
 fprintf(stderr,"CANcase XL: resume boot query timed out; one USB device reset\n");
 if(ioctl(fd,USBDEVFS_RESET,NULL)) die("resume USB reset");
 select_config(1);
 command(boot,60,60);
}
/* Two observed bootcode variants; retain the original unknown-field gate. */
static void validate_boot_identity(void)
{
 static const unsigned char a[12]={0xc1,0,0,0,0x0c,0,0,3,0xa7,0,0,0};
 static const unsigned char b[12]={0xc1,0,0,0,0x0a,0,6,1,0xa7,0,0,0};
 if (!memcmp(reply,a,12) && (le32(reply+12)==0 || le32(reply+12)==9)) return;
 if (!memcmp(reply,b,12) && le32(reply+12)==9) return;
 die("bootloader identity");
}
static const char *piggy_name(unsigned type)
{
 switch(type) {
 case 0x101:return "CANpiggy 251 (Highspeed)";
 case 0x135:return "CANpiggy 251mag (Highspeed)";
 case 0x136:return "CANpiggy 1050mag (Highspeed)";
 default:return "unknown";
 }
}
/* Read only the identity block observed in both Windows captures. No licence
 * decoding, memory scans, persistent writes or CAN start commands. */
static void device_identity(void)
{
 unsigned char select[24]={0x25,0,0,0,0,1};
 unsigned char read[18]={0x10,0,0,0,1,0,0,0,0,0x42,0,0,8};
 select[23]=0xff;
 command(select,sizeof(select),24);
 command(read,sizeof(read),25);
 if (memcmp(reply+4,read+4,12)) die("identity header address/length");
 read[8]=8; read[12]=0xb0;
 command(read,sizeof(read),193);
 if (memcmp(reply+4,read+4,12) ||
     memcmp(reply+56,"Vector",6) || memcmp(reply+88,"CANcaseXL",9))
  die("identity block layout");
 fprintf(stderr,"CANcase XL identity: serial=%06u article=%06u (read-only)\n",
         le32(reply+152),le32(reply+174));
}
static void channel_report(unsigned channel){unsigned char data[65536];double end=now()+2;int found=0;while(now()<end&&!found){int n=bulk(0x82,data,sizeof(data),100);if(n<0){if(errno==ETIMEDOUT)continue;die("channel report read");}unsigned pos=0;while(pos<(unsigned)n){if((unsigned)n-pos<4)die("report header");unsigned count=le32(data+pos);if(count>21)die("report count");unsigned used=4+24*count;unsigned remain=(unsigned)n-pos;unsigned span=remain>=512?512:used;if(span>remain||used>span)die("report framing");for(unsigned j=0;j<count;j++){unsigned char *r=data+pos+4+24*j;if(r[0]==5&&(r[1]&127)==channel){found=1;unsigned type=le16(r+18);fprintf(stderr,"CANcase XL channel %u: piggy_type=0x%04x name=%s transceiver_state=0x%04x\n",channel+1,type,piggy_name(type),le16(r+20));}}for(unsigned j=used;j<span;j++)if(data[pos+j])die("report padding");pos+=span;}}if(!found)die("channel initialization report missing");}
int main(int argc,char **argv){int resume=argc==5&&!strcmp(argv[1],"--resume");if(resume){argc--;argv++;}if(argc==4&&!strcmp(argv[1],"--extract")){extract_firmware(argv[2],argv[3]);return 0;}if(argc==3&&!strcmp(argv[1],"--check")){free(firmware(argv[2]));puts("Firmware hash verified (SYS or raw image)");return 0;}if(argc!=4){fprintf(stderr,"Usage: %s BUS ADDRESS SYS_OR_BIN | --check SYS_OR_BIN | --extract SYS_OR_BIN OUTPUT_BIN\n",argv[0]);return 2;}char *end;long bus=strtol(argv[1],&end,10);if(*end||bus<1||bus>255)return 2;long address=strtol(argv[2],&end,10);if(*end||address<1||address>127)return 2;selected_bus=(unsigned)bus;selected_address=(unsigned)address;unsigned char *sys=firmware(argv[3]);char path[80];snprintf(path,sizeof(path),"/dev/bus/usb/%03ld/%03ld",bus,address);fd=open(path,O_RDWR|O_CLOEXEC);if(fd<0)die("open USB");unsigned char config;control(0x80,8,0,&config,1);if(config>2)die("unknown configuration");descriptors(config ? config : 1);int cold=config!=2;if(!cold){select_config(2);cold=!runtime_present();}if(cold){select_config(1);boot_identity(resume);validate_boot_identity();for(unsigned off=0;off<LENGTH;off+=1265){unsigned size=LENGTH-off;if(size>1265)size=1265;unsigned char packet[1280]={0x40};packet[2]=(off+size==LENGTH);put32(packet+6,0x15000+off);put32(packet+10,size);memcpy(packet+14,sys+off,size);command(packet,15+size,6);}select_config(0);control(0x40,1,1,NULL,0);struct timespec delay={0,100000000};nanosleep(&delay,NULL);select_config(2);}
 if(cold) { simple(0x33,0,2); }
 simple(0x2a,0,8);unsigned char version[24]={0x24};command(version,24,24);if(le32(reply+4)!=8||le32(reply+8)!=7||le32(reply+12)!=16)die("runtime version");if(cold)for(unsigned c=0;c<2;c++){simple(4,c,6);channel_report(c);}if(cold)device_identity();simple(5,1,4);simple(0x31,0,8);unsigned char timer[8]={0x2a,0,0,0,250};command(timer,8,6);for(unsigned c=0;c<2;c++){if(!cold)simple(0x19,c,4);simple(0x23,c,12);unsigned char filter[267]={0x22,0,0,0,1,0,0,0,0,1};filter[2]=(unsigned char)c;command(filter,sizeof(filter),6);}unsigned interface=0;if(ioctl(fd,USBDEVFS_RELEASEINTERFACE,&interface))die("release runtime");claimed=0;close(fd);free(sys);puts("Runtime firmware ready; both channels stopped (C loader).");return 0;}
