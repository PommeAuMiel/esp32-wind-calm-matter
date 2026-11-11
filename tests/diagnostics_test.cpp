// Diagnostic buffering uses SYNTHETIC packets only; no serial ports.
#include "Diagnostics.h"
#include <assert.h>
#include <stdio.h>
using namespace windcalm;
static void observe(void *ctx,const Frame &frame) { static_cast<Diagnostics *>(ctx)->capture(frame,42); }
int main() {
  Diagnostics d; Frame f; uint8_t payload[]={23,2,0,4,0,0,1,0xf4,100,1,0,1,1};
  assert(encode(0x22,payload,sizeof(payload),f,3));
  d.capture(f,1); DiagnosticEvent event; assert(!d.pop(event));
  d.setEnabled(true);
  for(int i=0;i<10000;++i) d.capture(f,uint32_t(i));
  assert(d.dropped==9996);
  for(int i=0;i<4;++i) { assert(d.pop(event)); assert(event.time==unsigned(i) && event.sequence==unsigned(i+1)); }
  assert(!d.pop(event));
  DatapointCursor cursor(payload,sizeof(payload)); DatapointView dp;
  assert(cursor.next(dp) && dp.id==23 && dp.type==2 && dp.length==4 && dp.value[3]==0xf4);
  assert(cursor.next(dp) && dp.id==100 && dp.type==1 && dp.length==1 && dp.value[0]==1);
  assert(!cursor.next(dp) && !cursor.malformed);
  for(size_t len=1;len<8;++len) {
    DatapointCursor truncated(payload,len); assert(!truncated.next(dp) && truncated.malformed);
  }
  uint8_t huge[]={90,0,0xff,0xff}; DatapointCursor bad(huge,sizeof(huge));
  assert(!bad.next(dp) && bad.malformed);
  d.capture(f,8); d.setEnabled(false); assert(!d.pop(event));
  // Observation does not emit arbitrary controls; only normal ACK22/23.
  Session session; session.setObserver(observe,&d); d.setEnabled(true);
  for(size_t i=0;i<f.size;++i) session.feed(f.bytes[i],42);
  assert(d.pop(event) && event.frame.command()==0x22);
  Frame response; assert(session.pop(response) && response.command()==0x23 && response.payload()[0]==1);
  assert(!session.pop(response) && !session.ready());
  f.bytes[f.size-1]^=1;
  for(size_t i=0;i<f.size;++i) session.feed(f.bytes[i],43);
  assert(!d.pop(event) && !session.pop(response)); // Bad checksum never appears as valid report.
  d.setEnabled(false);
  d.setEnabled(true, 0xfffffff0u); d.capture(f, 0xfffffff0u);
  d.tick(119983); assert(d.enabled);
  d.tick(119984); assert(!d.enabled && !d.pop(event));
  d.setEnabled(true, 200000); d.tick(200001); assert(d.enabled);
  d.setEnabled(false);
  puts("Diagnostics: bounded ring/overflow, unknown DP23/100, truncation, observer ACK isolation and invalid checksum passed");
}
