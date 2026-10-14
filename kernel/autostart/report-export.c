// SPDX-License-Identifier: GPL-2.0
/* Offline export of cached sysfs information. No USB access or hardware writes. */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <dirent.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
typedef uint8_t u8;
typedef uint32_t u32;
#include "../license_decode.h"

struct channel {
 char interface[64], serial[32], article[32], channel[16], firmware[64];
 char piggy[32], name[128], state[32], licenses[2048], oem[64];
 int updates; /* -1: older module without this sysfs attribute */
 u8 mask[32];
};
static void fail(const char *s) { fprintf(stderr,"Export: %s (%s)\n",s,strerror(errno)); exit(1); }
static void invalid(const char *s) { fprintf(stderr,"Export: %s\n",s); exit(1); }
static void field(const char *root,const char *iface,const char *name,char *out,size_t size) {
 char *path; if(asprintf(&path,"%s/%s/cancasexl/%s",root,iface,name)<0) fail("memory");
 FILE *f=fopen(path,"r");free(path);if(!f) fail(name);
 size_t n=fread(out,1,size-1,f);if(ferror(f)||!feof(f)) invalid("Metadata too long or read error");
 fclose(f);out[n]=0;while(n && (out[n-1]=='\n'||out[n-1]=='\r'))out[--n]=0;
}
static void esc(FILE *f,const char *s,int xml) {
 if(!xml)fputc('"',f);
 for(const unsigned char *p=(const unsigned char *)s;*p;p++) {
  if(xml) {
   switch(*p){case '&':fputs("&amp;",f);break;case '<':fputs("&lt;",f);break;case '>':fputs("&gt;",f);break;
    default:if(*p<32 && *p!=9 && *p!=10 && *p!=13)invalid("Invalid XML character");fputc(*p,f);}
  } else if(*p=='"'||*p=='\\')fprintf(f,"\\%c",*p);
  else if(*p<32)fprintf(f,"\\u%04x",*p);else fputc(*p,f);
 }
 if(!xml)fputc('"',f);
}
static void tag(FILE *f,const char *t,const char *v) {fprintf(f,"<%s>",t);esc(f,v,1);fprintf(f,"</%s>\n",t);}
static void pair(FILE *f,const char *k,const char *v) {esc(f,k,0);fputc(':',f);esc(f,v,0);}
static const char *lname(unsigned id) {
 switch(id){case 1:return "CANalyzer";case 12:return "CANape >= 5.6";
 case 37:return "Application DENalyzer PRO";case 44:return "CAN for DENalyzer PRO";
 case 97:return "CANape >= 8.0 / CANdito >= 4.0";default:return "Unknown license";}
}
static void init_cipher(struct xl_lic_cipher *c,const u8 *key,unsigned n) {
 u32 l=0,r=0;
 for(unsigned i=0;i<18;i++){u32 w=0;for(unsigned j=0;j<4;j++)w|=(u32)key[(4*i+j)%n]<<(8*j);c->p[i]=xl_lic_initial[i]^w;}
 for(unsigned i=0;i<1024;i++)c->s[i]=xl_lic_initial[18+i];
 for(unsigned i=0;i<18;i+=2){xl_lic_block(c,&l,&r,0);c->p[i]=l;c->p[i+1]=r;}
 for(unsigned i=0;i<1024;i+=2){xl_lic_block(c,&l,&r,0);c->s[i]=l;c->s[i+1]=r;}
}
static void encrypt(struct xl_lic_cipher *c,u8 *buf,size_t n) {
 for(size_t i=0;i<n;i+=8){u32 l=xl_lic_le(buf+i),r=xl_lic_le(buf+i+4);xl_lic_block(c,&l,&r,0);
  for(unsigned j=0;j<4;j++){buf[i+j]=(u8)(l>>(8*j));buf[i+4+j]=(u8)(r>>(8*j));}}
}
static int write_all(int fd,const void *data,size_t n) {
 const char *p=data;while(n){ssize_t m=write(fd,p,n);if(m<0&&errno==EINTR)continue;if(m<=0)return -1;p+=m;n-=(size_t)m;}return 0;
}
int main(int argc,char **argv) {
 (void)xl_oem_decode;
 (void)xl_license_decode; /* shared primitive header also contains hardware decoder */
 if(argc<2||argc>3)invalid("Usage: report-export PREFIX [SERIAL]");
 const char *root=getenv("CANCASEXL_NET_SYSFS");if(!root)root="/sys/class/net";
 DIR *dir=opendir(root);if(!dir)fail("sysfs");struct dirent *entry;
 struct channel channels[64];unsigned count=0;
 while((entry=readdir(dir))) {
  char *path;if(asprintf(&path,"%s/%s/cancasexl",root,entry->d_name)<0)fail("memory");
  struct stat st;int exists=!stat(path,&st)&&S_ISDIR(st.st_mode);free(path);if(!exists)continue;
  struct channel c={.updates=-1};if(strlen(entry->d_name)>=sizeof(c.interface))invalid("Interface name too long");
  strcpy(c.interface,entry->d_name);
  field(root,c.interface,"serial",c.serial,sizeof(c.serial));
  if(argc==3&&strcmp(argv[2],c.serial))continue;
#define READ(member,file) field(root,c.interface,file,c.member,sizeof(c.member))
  READ(article,"article");READ(channel,"channel");READ(firmware,"firmware_version");
  READ(piggy,"piggy_type");READ(name,"piggy_name");READ(state,"transceiver_state_initial");READ(licenses,"licenses");
  char *op;if(asprintf(&op,"%s/%s/cancasexl/oem_codes",root,c.interface)<0)fail("memory");
  if(!access(op,F_OK)) {READ(oem,"oem_codes");} else strcpy(c.oem,"unavailable");free(op);
  if(strcmp(c.oem,"unavailable")) {
   if(strlen(c.oem)!=33||c.oem[16]!=' ')invalid("Invalid OEM mask");
   for(unsigned z=0;z<33;z++)if(z!=16&&!((c.oem[z]>='0'&&c.oem[z]<='9')||(c.oem[z]>='a'&&c.oem[z]<='f')))invalid("Invalid OEM mask");
  }
  char *up;if(asprintf(&up,"%s/%s/cancasexl/license_updates",root,c.interface)<0)fail("memory");
  if(!access(up,F_OK)) {
   char value[16];field(root,c.interface,"license_updates",value,sizeof(value));
   size_t n=strlen(value);
   if(!n||n>5||(n>1&&value[0]=='0')||strspn(value,"0123456789")!=n)invalid("Invalid license update count");
   unsigned long v=strtoul(value,NULL,10);if(v>65535)invalid("Invalid license update count");
   c.updates=(int)v;
  } else if(errno!=ENOENT)fail("license_updates");
  free(up);
#undef READ
  char list[2048];strcpy(list,c.licenses);char *save=NULL;
  for(char *v=strtok_r(list," ", &save);v;v=strtok_r(NULL," ",&save)) {
   char *end;errno=0;unsigned long id=strtoul(v,&end,16);
   if(errno||*end||strlen(v)!=5||strncmp(v,"0x",2)||id<1||id>256)invalid("No verified license data; export aborted");
   c.mask[(id-1)/8]|=(u8)(1U<<((id-1)%8));
  }
  if(count && (strcmp(channels[0].serial,c.serial)))invalid("Multiple devices: specify --serial");
  if(count && (strcmp(channels[0].article,c.article)||strcmp(channels[0].firmware,c.firmware)||memcmp(channels[0].mask,c.mask,32)||strcmp(channels[0].oem,c.oem)||channels[0].updates!=c.updates))invalid("Inconsistent device metadata");
  if(count>=64)invalid("Too many channels");
  channels[count++]=c;
 }
 closedir(dir);if(!count)invalid("No matching device found");
 for(unsigned i=0;i<count;i++)for(unsigned j=i+1;j<count;j++)if(strcmp(channels[i].channel,channels[j].channel)>0){struct channel c=channels[i];channels[i]=channels[j];channels[j]=c;}
 struct channel *c=&channels[0];
 time_t now=time(NULL);struct tm tm;if(now==(time_t)-1||!localtime_r(&now,&tm))fail("time");
 char date[32],stamp[64];strftime(date,sizeof(date),"%Y/%m/%d",&tm);strftime(stamp,sizeof(stamp),"%Y-%m-%dT%H:%M:%S%z",&tm);
 char *xml=NULL,*json=NULL;size_t xn=0,jn=0;FILE *x=open_memstream(&xml,&xn),*j=open_memstream(&json,&jn);if(!x||!j)fail("memory");
 fputs("<VECTOR_HARDWARE>\n<!-- Generated by cancasexl-linux; cached Linux data, not a Vector Hardware Config export. Update count is included when available; OEM fields are present only when verified. -->\n",x);
 tag(x,"SOURCE","cancasexl-linux 0.5.31 / probe_cache");tag(x,"DATE",date);
 fputs("<DEVICE>\n",x);tag(x,"DEVICENAME","CANcaseXL");tag(x,"SERIALNR",c->serial);tag(x,"ARTICLENR",c->article);tag(x,"DRVVERSION",c->firmware);
 fputs("<LICENSE>\n",x);
 for(unsigned base=0;base<32;base+=8){fprintf(x,"<CODE pos='%u'>",base);for(unsigned k=8;k>0;k--)fprintf(x,"%02x",c->mask[base+k-1]);fputs("</CODE>\n",x);}
 if(strcmp(c->oem,"unavailable"))fprintf(x,"<OEMCODE pos='0'>%.16s</OEMCODE>\n<OEMCODE pos='8'>%.16s</OEMCODE>\n",c->oem,c->oem+17);
 if(c->updates>=0)fprintf(x,"<UPDATES>%d</UPDATES>\n",c->updates);
 fputs("<APPNAME><ROOTNAME>Reported licenses</ROOTNAME>\n",x);
 for(unsigned id=1;id<=256;id++)if(c->mask[(id-1)/8]&(1U<<((id-1)%8)))tag(x,"NAME",lname(id));
 fputs("</APPNAME>\n</LICENSE>\n</DEVICE>\n</VECTOR_HARDWARE>\n",x);
 fputs("{\n",j);pair(j,"source","cancasexl-linux");fputs(",\n",j);pair(j,"exported_at",stamp);
 fputs(",\n",j);pair(j,"license_source","probe_cache");fputs(",\n",j);pair(j,"serial",c->serial);fputs(",\n",j);pair(j,"article",c->article);
 fputs(",\n",j);pair(j,"firmware_version",c->firmware);
 fputs(",\n\"license_status\":\"verified\",\"updates\":",j);
 if(c->updates>=0)fprintf(j,"%d",c->updates);else fputs("null",j);
 fputs(",\"license_codes\":[",j);
 for(unsigned base=0;base<32;base+=8){if(base)fputc(',',j);fprintf(j,"{\"pos\":%u,\"hex\":\"",base);for(unsigned k=8;k>0;k--)fprintf(j,"%02x",c->mask[base+k-1]);fputs("\"}",j);}
 fputs("],\n\"oem_codes\":",j);
 if(!strcmp(c->oem,"unavailable"))fputs("null",j);
 else fprintf(j,"[{\"pos\":0,\"hex\":\"%.16s\"},{\"pos\":8,\"hex\":\"%.16s\"}]",c->oem,c->oem+17);
 fputs(",\n\"oem_status\":",j);esc(j,!strcmp(c->oem,"unavailable")?"unavailable":"verified",0);
 fputs(",\n\"licenses\":[",j);unsigned first=1;
 for(unsigned id=1;id<=256;id++)if(c->mask[(id-1)/8]&(1U<<((id-1)%8))){if(!first)fputc(',',j);first=0;fprintf(j,"{\"id\":\"0x%03X\",\"name\":",id);esc(j,lname(id),0);fputc('}',j);}
 fputs("],\n\"interfaces\":[",j);
 for(unsigned i=0;i<count;i++){if(i)fputc(',',j);fputc('{',j);
  pair(j,"interface",channels[i].interface);fputc(',',j);pair(j,"channel",channels[i].channel);fputc(',',j);pair(j,"piggy_type",channels[i].piggy);
  fputc(',',j);pair(j,"piggy_name",channels[i].name);fputc(',',j);pair(j,"transceiver_state_initial",channels[i].state);fputc('}',j);}
 fputs("]\n}\n",j);if(fclose(x)||fclose(j))fail("create report");
 /* Recheck the cached identity and license fields before publishing. */
 for(unsigned i=0;i<count;i++){char value[2048];field(root,channels[i].interface,"serial",value,sizeof(value));if(strcmp(value,c->serial))invalid("Device changed during export");field(root,channels[i].interface,"licenses",value,sizeof(value));if(strcmp(value,channels[i].licenses))invalid("Licenses changed during export");}
 struct xl_lic_cipher cipher;char key0[16],seed[16],key[17];
 snprintf(key0,sizeof(key0),"\n#A{3%02x",tm.tm_sec);snprintf(seed,sizeof(seed),"%02x6\"%02x%02x",(tm.tm_year+1900)&255,tm.tm_mon+1,tm.tm_sec);
 init_cipher(&cipher,(const u8 *)key0,(unsigned)strlen(key0));encrypt(&cipher,(u8 *)seed,8);
 for(unsigned i=0;i<8;i++)snprintf(key+2*i,3,"%02x",(unsigned char)seed[i]);
 init_cipher(&cipher,(const u8 *)key,16);size_t size=(xn/8+1)*8;u8 *blob=calloc(size+1,1);if(!blob)fail("memory");blob[0]=(u8)tm.tm_sec;memcpy(blob+1,xml,xn);encrypt(&cipher,blob+1,size);
 char *vp,*jp;if(asprintf(&vp,"%s.vlurep",argv[1])<0||asprintf(&jp,"%s.json",argv[1])<0)fail("memory");
 int vf=open(vp,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(vf<0)fail(vp);
 int jf=open(jp,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(jf<0){int err=errno;close(vf);unlink(vp);errno=err;fail(jp);}
 int failed=write_all(vf,blob,size+1)||write_all(jf,json,jn);if(fsync(vf)||fsync(jf))failed=1;
 if(close(vf))failed=1;
 if(close(jf))failed=1;
 if(failed){unlink(vp);unlink(jp);invalid("Write error; incomplete output removed");}
 printf("Export: %s\nPlain text: %s\n",vp,jp);free(vp);free(jp);free(blob);free(xml);free(json);return 0;
}
