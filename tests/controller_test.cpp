// ALL received packets here are SYNTHETIC, not captures from the user's fan.
#include "Controller.h"
#include <assert.h>
#include <stdio.h>
#include <vector>
using namespace windcalm;
using Bytes = std::vector<uint8_t>;
static void report(Controller &c, uint8_t cmd, const Bytes &dp, uint32_t now) {
  Frame f; assert(encode(cmd, dp.data(), dp.size(), f, 3));
  for (size_t i=0;i<f.size;++i) c.feed(f.bytes[i],now);
}
static Frame pop(Controller &c, uint8_t cmd) {
  Frame f; assert(c.pop(f) && f.command()==cmd); return f;
}
static void empty(Controller &c) { Frame f; assert(!c.pop(f)); }
static void init(Controller &c, uint32_t now=1) {
  c.start(now); pop(c,0); report(c,0,{0},now); pop(c,1);
  report(c,1,{'{','}'},now); pop(c,2); report(c,2,{5,6},now); pop(c,8);
  report(c,7,{20,1,0,1,0,60,1,0,1,0,62,2,0,4,0,0,0,3,63,4,0,1,0},now);
  assert(c.session.ready() && c.level()==0); empty(c);
}
int main() {
  Controller c; c.start(0); pop(c,0);
  assert(!c.requestFan(3,1) && !c.requestLight(true,1)); empty(c);
  init(c); assert(!c.fanPending && !c.lightPending);
  // Manual diagnosis emits only one empty query and never changes confirmed state.
  {
    Controller diagnostic; assert(!diagnostic.requestReadback(1));
    init(diagnostic);
    assert(diagnostic.requestReadback(2)); assert(!diagnostic.requestReadback(3));
    Frame readback = pop(diagnostic,8); assert(readback.length()==0); empty(diagnostic);
    assert(!diagnostic.session.confirmed.light && !diagnostic.lightPending && diagnostic.requests==0);
    assert(!diagnostic.requestReadback(1001));
    assert(diagnostic.requestReadback(1002)); pop(diagnostic,8); empty(diagnostic);
    // Query stays behind explicit controls; it does not cancel pending commands.
    assert(diagnostic.requestLight(true,1003)); assert(diagnostic.requestReadback(2002));
    assert(pop(diagnostic,6).payload()[0]==20); pop(diagnostic,8); empty(diagnostic);
    assert(diagnostic.lightPending && !diagnostic.session.confirmed.light);
    report(diagnostic,7,{20,1,0,1,1},2003);
    assert(!diagnostic.lightPending && diagnostic.session.confirmed.light);
    // A reboot removes queued readback and resets its limiter without auto-controls.
    assert(diagnostic.requestReadback(3002)); report(diagnostic,0,{0},3003); pop(diagnostic,1); empty(diagnostic);
    assert(!diagnostic.requestReadback(3004));
    init(diagnostic,0xfffffff0u); assert(diagnostic.requestReadback(0xfffffff0u)); pop(diagnostic,8); empty(diagnostic);
    assert(!diagnostic.requestReadback(983)); assert(diagnostic.requestReadback(984)); pop(diagnostic,8); empty(diagnostic);
  }
  // Requested values must never masquerade as a controller confirmation.
  assert(c.requestFan(6,10)); Frame speed=pop(c,6), on=pop(c,6);
  assert(speed.payload()[0]==62 && speed.payload()[7]==6);
  assert(on.payload()[0]==60 && on.payload()[4]==1); empty(c);
  assert(c.level()==0 && c.fanPending);
  // Fragmented confirmations arrive as separate reports, in either order.
  report(c,7,{62,2,0,4,0,0,0,6},11); assert(c.fanPending && c.level()==0); empty(c);
  report(c,0x22,{60,1,0,1,1},12); assert(!c.fanPending && c.level()==6);
  assert(c.confirmations==1); pop(c,0x23); empty(c);
  assert(c.requestFan(2,13)); pop(c,6); pop(c,6);
  report(c,7,{60,1,0,1,1},14); assert(c.fanPending);
  report(c,7,{62,2,0,4,0,0,0,2},15); assert(!c.fanPending && c.level()==2); empty(c);
  assert(c.requestFan(0,16)); assert(pop(c,6).payload()[0]==60); empty(c);
  report(c,7,{60,1,0,1,0},17); assert(!c.fanPending && c.level()==0 && c.lastLevel==2);
  // Light and fan have independent pending state; remote wins without echo.
  assert(c.requestFan(4,20) && c.requestLight(true,20)); pop(c,6); pop(c,6); pop(c,6);
  report(c,0x22,{20,1,0,1,0},21); pop(c,0x23);
  assert(!c.lightPending && c.fanPending && c.cancellations==1);
  report(c,7,{62,2,0,4,0,0,0,5},22); assert(!c.fanPending && c.cancellations==2); empty(c);
  assert(c.requestLight(true,30)); pop(c,6);
  report(c,0x22,{20,1,0,1,1},31); pop(c,0x23); assert(!c.lightPending && c.session.confirmed.light); empty(c);
  // Timeout only queries state once, never retries motor/light commands.
  assert(c.requestFan(1,100) && c.requestLight(false,100)); pop(c,6); pop(c,6); pop(c,6);
  c.tick(3099); assert(c.fanPending && c.lightPending); empty(c);
  c.tick(3100); assert(!c.fanPending && !c.lightPending && c.timeouts==2); pop(c,8); empty(c);
  c.tick(3101); empty(c);
  // MCU reboot cancels queued commands and pending readback, with no replay.
  assert(c.requestFan(3,3200)); report(c,0,{0},3201); pop(c,1); empty(c);
  assert(!c.fanPending && !c.session.ready());
  report(c,7,{60,1,0,1,1},3202); empty(c); assert(c.session.confirmed.known==0);
  init(c,4000); assert(c.requestLight(true,4001)); pop(c,6);
  c.tick(49000); pop(c,0); empty(c); assert(!c.lightPending && !c.session.ready());
  // Explicit status 03 for the empty-configuration branch, without dependence on HA.
  c.start(50000); pop(c,0); report(c,0,{1},50001); pop(c,1);
  report(c,1,{'{','}'},50002); pop(c,2); report(c,2,{},50003);
  c.tick(50004); assert(pop(c,3).payload()[0]==3); empty(c);
  report(c,3,{},50005); pop(c,8); assert(!c.session.ready());
  // Millis rollover of command confirmation deadline.
  init(c,0xfffffff0u); assert(c.requestLight(true,0xfffffff0u)); pop(c,6);
  c.tick(2983); assert(c.lightPending); empty(c);
  c.tick(2984); assert(!c.lightPending); pop(c,8); empty(c);
  assert(!c.requestFan(7,2985));
  for(unsigned p=0;p<=100;++p) {
    uint8_t level=Controller::levelFor(uint8_t(p));
    assert(level<=6 && Controller::percentFor(level)>=p);
  }
  // Reverse uses enum DP63, only after reported OFF has lasted 10s.
  init(c,1); assert(!c.mayReverse(10000));
  assert(!c.requestDirection(1,10000)); empty(c);
  assert(c.mayReverse(10001)); assert(!c.requestDirection(2,10001));
  assert(c.requestDirection(1,10001));
  Frame reverse=pop(c,6);
  const Bytes golden={0x55,0xaa,0,6,0,5,63,4,0,1,1,0x4f};
  assert(Bytes(reverse.bytes,reverse.bytes+reverse.size)==golden);
  assert(c.directionPending && c.session.confirmed.direction==0);
  assert(!c.requestFan(1,10002)); empty(c); // No start while changing direction.
  report(c,0x22,{63,4,0,1,1},10003); pop(c,0x23);
  assert(!c.directionPending && c.session.confirmed.direction==1); empty(c); // No auto-start.
  assert(c.requestDirection(1,10004)); empty(c); // Same direction does not transmit.
  assert(c.requestFan(1,10005)); pop(c,6); pop(c,6);
  report(c,7,{60,1,0,1,1,62,2,0,4,0,0,0,1},10006);
  assert(!c.mayReverse(30000) && !c.requestDirection(0,30000)); empty(c);
  assert(!c.session.prepareControl(63,0,true)); // Session also rejects a running fan.
  // An OFF request alone cannot bypass the guard before its report.
  assert(c.requestFan(0,10007)); pop(c,6);
  assert(!c.requestDirection(0,30000)); empty(c);
  report(c,7,{60,1,0,1,0},10008);
  assert(!c.mayReverse(20007)); assert(c.mayReverse(20008));
  // Lost reverse confirmation invalidates direction and prohibits startup/replay.
  init(c,1); assert(c.requestDirection(1,10001)); pop(c,6);
  c.tick(13000); assert(c.directionPending); empty(c);
  c.tick(13001); assert(!c.directionPending && !c.session.ready()); pop(c,8); empty(c);
  assert(!c.requestFan(1,13002) && !c.requestDirection(1,13002)); empty(c);
  report(c,7,{63,4,0,1,1},13003); assert(c.session.ready()); empty(c);
  assert(!c.mayReverse(23002) && c.mayReverse(23003));
  // A reboot cancels unsent reverse and requires a new stop dwell after sync.
  assert(c.requestDirection(0,23003));
  report(c,0,{0},23004); pop(c,1); empty(c);
  assert(!c.directionPending && !c.mayReverse(50000));
  init(c,0xfffffff0u); assert(!c.mayReverse(9983)); assert(c.mayReverse(9984));
  assert(c.requestDirection(1,9984)); pop(c,6);
  report(c,7,{63,4,0,1,0},9985); assert(!c.directionPending); empty(c); // Remote wins.
  // Reconstructed fixtures from captured DP metadata, not raw serial captures.
  init(c,1);
  assert(!c.requestTemperature(500,2) && !c.requestBeeps(true,2)); empty(c);
  report(c,7,{23,2,0,4,0,0,1,244,66,1,0,1,1},3);
  assert(c.session.confirmed.temperature==500 && c.session.confirmed.beeps);
  assert((c.session.confirmed.known & 48)==48); empty(c);
  assert(!c.requestTemperature(1,4) && !c.requestTemperature(1001,4)); empty(c);
  assert(c.requestTemperature(1000,5)); Frame temp=pop(c,6);
  assert(temp.length()==8 && temp.payload()[0]==23 && temp.payload()[1]==2);
  assert(temp.payload()[6]==3 && temp.payload()[7]==232);
  assert(c.temperaturePending && c.session.confirmed.temperature==500); empty(c);
  report(c,7,{23,2,0,4,0,0,3,232},6);
  assert(!c.temperaturePending && c.session.confirmed.temperature==1000); empty(c);
  assert(c.requestBeeps(false,7)); Frame mute=pop(c,6);
  assert(mute.length()==5 && mute.payload()[0]==66 && mute.payload()[1]==1 && mute.payload()[4]==0);
  assert(c.beepsPending && c.session.confirmed.beeps); empty(c);
  report(c,0x22,{66,1,0,1,0},8); pop(c,0x23);
  assert(!c.beepsPending && !c.session.confirmed.beeps); empty(c);
  // A malformed trailing known DP leaves the entire snapshot unchanged.
  report(c,7,{23,2,0,4,0,0,0,0,66,4,0,1,1},9);
  assert(c.session.confirmed.temperature==1000 && !c.session.confirmed.beeps); empty(c);
  report(c,7,{23,2,0,4,0,0,3,233},10);
  assert(c.session.confirmed.temperature==1000); empty(c);
  assert(c.requestTemperature(0,20) && c.requestBeeps(true,20)); pop(c,6); pop(c,6);
  report(c,7,{23,2,0,4,0,0,1,244},21); // Remote overrides requested preset.
  assert(!c.temperaturePending && c.session.confirmed.temperature==500 && c.beepsPending);
  c.tick(3020); assert(!c.beepsPending && !(c.session.confirmed.known & 32)); pop(c,8); empty(c);
  assert(!c.requestBeeps(false,3021)); empty(c); // No replay after lost readback.
  report(c,7,{66,1,0,1,1},3022); assert(c.requestBeeps(false,3023));
  report(c,0,{0},3024); pop(c,1); empty(c);
  assert(!c.beepsPending && !c.temperaturePending && !(c.session.confirmed.known & 48));
  puts("Controller: captured-preset schema, bool mute, confirmed readback, remote override, atomic validation, timeout and reboot passed");
  puts("Controller: reverse stop dwell, enum golden frame, no auto-start, running/pending gate, timeout readback, reboot and rollover passed");
  puts("Controller: startup gate, real-report confirmation, partial DPs, remote override, independent endpoints, timeout/readback, restart and rollover passed");
}
