// ALL MCU inputs below are SYNTHETIC fixtures, never hardware captures.
#include "TuyaProtocol.h"
#include <assert.h>
#include <stdio.h>
#include <vector>
using namespace windcalm;
using Bytes = std::vector<uint8_t>;
static const Bytes allDP = {20,1,0,1,1, 60,1,0,1,0, 62,2,0,4,0,0,0,3, 63,4,0,1,1};
static void collect(void *context, const Frame &f) {
  static_cast<std::vector<Frame> *>(context)->push_back(f);
}
static Frame mcu(uint8_t cmd, const Bytes &data = {}) {
  Frame f; assert(encode(cmd, data.data(), data.size(), f, 3)); return f;
}
static void deliver(Session &s, const Frame &f, uint32_t now = 1) {
  for (size_t i = 0; i < f.size; ++i) s.feed(f.bytes[i], now);
}
static Frame pop(Session &s, uint8_t cmd) {
  Frame f; assert(s.pop(f)); assert(f.command() == cmd); return f;
}
static void empty(Session &s) { Frame f; assert(!s.pop(f)); }
static void initialize(Session &s, uint32_t now = 1) {
  s.start(now); pop(s,0x00);
  deliver(s,mcu(0x00,{0}),now); pop(s,0x01);
  deliver(s,mcu(0x01,{'{','}'}),now); pop(s,0x02);
  deliver(s,mcu(0x02,{5,6}),now); pop(s,0x08);
  assert(!s.ready());
  deliver(s,mcu(0x07,allDP),now); assert(s.ready()); empty(s);
}
int main() {
  // Independent literal golden fixtures, with fixed checksums.
  const Bytes lightReport = {0x55,0xaa,0x03,0x22,0x00,0x05,0x14,0x01,0x00,0x01,0x01,0x40};
  const Bytes ackSuccess = {0x55,0xaa,0x00,0x23,0x00,0x01,0x01,0x24};
  Frame encoded; const uint8_t one = 1;
  assert(encode(0x23,&one,1,encoded));
  assert(Bytes(encoded.bytes,encoded.bytes+encoded.size) == ackSuccess);
  assert(!encode(0,nullptr,1,encoded)); assert(!encode(0,&one,MaxPayload+1,encoded));
  for (size_t split = 0; split <= lightReport.size(); ++split) {
    std::vector<Frame> frames; Parser p(collect,&frames);
    for (size_t i=0;i<split;++i) p.feed(lightReport[i],1);
    for (size_t i=split;i<lightReport.size();++i) p.feed(lightReport[i],200);
    assert(frames.size()==1); assert(p.buffered()==0);
  }
  // Noise, concatenation, checksum failure, overlapping magic, oversized length.
  std::vector<Frame> frames; Parser p(collect,&frames);
  Bytes stream = {0,0xaa,0x55,0x55,0xaa,3,7,0xff,0xff};
  Bytes bad=lightReport; bad.back()^=1;
  stream.insert(stream.end(),bad.begin(),bad.end());
  stream.insert(stream.end(),lightReport.begin(),lightReport.end());
  stream.insert(stream.end(),lightReport.begin(),lightReport.end());
  for (uint8_t b:stream) p.feed(b,1);
  assert(frames.size()==2 && p.stats.lengthErrors && p.stats.checksumErrors);
  // Truncated frame enclosing a later valid frame: recovered on idle timeout.
  frames.clear(); p.reset();
  for (uint8_t b:Bytes{0x55,0xaa,3,7,0,100,0}) p.feed(b,10);
  for (uint8_t b:lightReport) p.feed(b,11);
  assert(frames.empty()); p.tick(261); assert(frames.size()==1 && p.buffered()==0);
  // Header magic inside a valid payload must not interrupt that frame.
  Frame embedded=mcu(7,{90,0,0,2,0x55,0xaa});
  frames.clear(); for(size_t i=0;i<embedded.size;++i) p.feed(embedded.bytes[i],300);
  assert(frames.size()==1 && frames[0].length()==6);
  // Whole-frame deadline even if every byte arrives before inter-byte timeout.
  p.reset(); for(uint8_t b:Bytes{0x55,0xaa,3,7,0,100}) p.feed(b,500);
  p.feed(0,700); p.feed(0,900); p.feed(0,1100); p.feed(0,1300); p.tick(1500);
  assert(p.buffered()==0);
  // Unsigned rollover timeout.
  p.feed(0x55,0xfffffff0u); p.tick(240); assert(p.buffered()==0);
  // Deterministic malformed-stream stress; bound and recovery under sanitizers.
  uint32_t random=0x531fa;
  for(uint32_t i=0;i<100000;++i) {
    random=random*1664525u+1013904223u; p.feed(uint8_t(random>>24),i+1000);
    assert(p.buffered()<MaxFrame);
  }
  p.tick(102000); frames.clear();
  for(uint8_t b:lightReport) p.feed(b,103000);
  assert(frames.size()==1);
  Bytes maximum(MaxPayload, 0x55); Frame large=mcu(7,maximum);
  frames.clear(); for(size_t i=0;i<large.size;++i) p.feed(large.bytes[i],104000);
  assert(frames.size()==1 && frames[0].length()==MaxPayload && p.buffered()==0);
  puts("Tuya: framing, fragmentation, recovery, deadlines and 100000-byte stress passed");

  Snapshot snapshot; assert(decodeDatapoints(allDP.data(),allDP.size(),snapshot));
  assert(snapshot.complete() && snapshot.light && !snapshot.fan && snapshot.speed==3 && snapshot.direction==1);
  Bytes invalid={20,1,0,1,0, 62,2,0,4,0,0,0,7};
  assert(!decodeDatapoints(invalid.data(),invalid.size(),snapshot)); assert(snapshot.light);
  for(const Bytes &dp:std::vector<Bytes>{{20,1,0,1,2},{60,4,0,1,1},{62,2,0,3,0,0,1},
      {63,4,0,1,2},{63,1,0,1,1},{90,6,0,0},{90,5,0,3,0,0,0},{20,1,0,9,1},{20}}) {
    assert(!decodeDatapoints(dp.data(),dp.size(),snapshot)); assert(snapshot.light);
  }
  const Bytes ignored={64,2,0,4,0,0,0,1,90,3,0,2,'o','k',91,5,0,2,1,2};
  assert(decodeDatapoints(ignored.data(),ignored.size(),snapshot)); assert(snapshot.speed==3);
  for(uint8_t level=1;level<=6;++level) {
    Bytes speed={62,2,0,4,0,0,0,level}; assert(decodeDatapoints(speed.data(),speed.size(),snapshot));
    assert(snapshot.speed==level && !snapshot.fan);
  }
  puts("Tuya: typed atomic DP decoding, six speeds, OFF memory and ignored timer passed");

  Session s; initialize(s);
  for(uint8_t b:lightReport) s.feed(b,2);
  Frame ack=pop(s,0x23); assert(Bytes(ack.bytes,ack.bytes+ack.size)==ackSuccess); empty(s);
  deliver(s,mcu(0x22,invalid),3); ack=pop(s,0x23); assert(ack.payload()[0]==0 && s.confirmed.light); empty(s);
  const Bytes ackFailure={0x55,0xaa,0x00,0x23,0x00,0x01,0x00,0x23};
  assert(Bytes(ack.bytes,ack.bytes+ack.size)==ackFailure);
  deliver(s,mcu(7,invalid),3); assert(s.confirmed.light && s.ready()); empty(s);
  deliver(s,mcu(0x22,{91,0,0,2,0x55,0xaa}),4); assert(pop(s,0x23).payload()[0]==1);
  // No ACK, no state changes for bad checksum or unsupported MCU version.
  Frame corrupt=mcu(0x22,{20,1,0,1,0}); corrupt.bytes[corrupt.size-1]^=1;
  deliver(s,corrupt,5); empty(s); assert(s.confirmed.light);
  assert(encode(0x22,invalid.data(),invalid.size(),corrupt,2)); deliver(s,corrupt,6); empty(s);
  deliver(s,mcu(0x99,{1}),7); assert(s.unknown==1); empty(s);
  // Explicit commands are gated; a prepared command is not a confirmation.
  assert(!s.prepareControl(60,1)); assert(!s.prepareControl(64,1,true));
  assert(!s.prepareControl(63,1)); assert(!s.prepareControl(63,2,true)); assert(!s.prepareControl(62,0,true));
  assert(!s.prepareControl(62,7,true)); assert(s.prepareControl(60,1,true));
  Frame cmd=pop(s,0x06); assert(cmd.payload()[0]==60 && cmd.payload()[4]==1);
  assert(!s.confirmed.fan); empty(s);
  deliver(s,mcu(7,{60,1,0,1,1}),8); assert(s.confirmed.fan); empty(s); // No echo.
  // Restart removes old motor requests and all confirmations; no automatic replay.
  assert(s.prepareControl(60,1,true));
  deliver(s,mcu(0,{0}),9); pop(s,1); empty(s);
  assert(!s.ready() && s.confirmed.known==0 && !s.prepareControl(60,1,true));
  deliver(s,mcu(7,allDP),10); assert(s.confirmed.known==0); empty(s);
  // Asynchronous unknown-only reports cannot mark a session ready.
  deliver(s,mcu(1,{'{','}'}),11); pop(s,2);
  deliver(s,mcu(2,{5,6}),12); pop(s,8);
  deliver(s,mcu(7,{90,4,0,1,1}),13); assert(!s.ready());
  deliver(s,mcu(7,{20,1,0,1,1}),14); assert(!s.ready());
  deliver(s,mcu(7,allDP),15); assert(s.ready()); empty(s);
  s.tick(15001); pop(s,0); empty(s); assert(s.ready());
  s.tick(45009); pop(s,0); empty(s); assert(!s.ready() && !s.confirmed.known);
  // Empty CONF requires explicit Wi-Fi mapping, without asserting Tuya cloud.
  s.start(50000); pop(s,0); deliver(s,mcu(0,{1}),50001); pop(s,1);
  deliver(s,mcu(1,{'{','}'}),50002); pop(s,2);
  deliver(s,mcu(2),50003); assert(s.phase==Phase::Network); empty(s);
  assert(!s.prepareNetworkStatus(5)); assert(s.prepareNetworkStatus(2));
  assert(pop(s,3).payload()[0]==2); deliver(s,mcu(3),50004); pop(s,8);
  assert(!s.ready()); deliver(s,mcu(7,allDP),50005); assert(s.ready());
  // Queue overflow invalidates state and removes every stale control.
  for(int i=0;i<8;++i) assert(s.prepareControl(60,1,true));
  assert(!s.prepareControl(60,1,true)); assert(s.queueOverflows==1 && !s.ready()); empty(s);
  // ACK allocation failure must not apply a synchronous report or claim success.
  initialize(s,60000);
  for(int i=0;i<8;++i) assert(s.prepareControl(60,1,true));
  deliver(s,mcu(0x22,{60,1,0,1,1}),60001); assert(!s.ready() && s.confirmed.known==0); empty(s);
  s.tick(61001); pop(s,0); empty(s);
  // Malformed negotiation replies never advance initialization.
  Session malformed; malformed.start(1); pop(malformed,0);
  deliver(malformed,mcu(0),2); assert(malformed.phase==Phase::Heartbeat); empty(malformed);
  deliver(malformed,mcu(0,{2}),3); assert(malformed.phase==Phase::Heartbeat); empty(malformed);
  deliver(malformed,mcu(0,{1}),4); pop(malformed,1);
  deliver(malformed,mcu(1,{0}),5); assert(malformed.phase==Phase::Product); empty(malformed);
  malformed.tick(1004); pop(malformed,1); empty(malformed);
  deliver(malformed,mcu(1,{'{','}'}),1005); pop(malformed,2);
  deliver(malformed,mcu(2,{1}),1006); assert(malformed.phase==Phase::Configuration); empty(malformed);
  puts("Tuya: sync ACK, handshake, explicit network mapping, heartbeat/restart and queue recovery passed");
}
