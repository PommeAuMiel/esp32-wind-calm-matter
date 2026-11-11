#include "../main/OtaPolicy.h"
#include <cassert>
#include <cstdio>
#include <limits>
using namespace windcalm_ota;
int main() {
  uint8_t digest[32]{};
  for (unsigned i=0;i<32;++i) digest[i]=uint8_t(i*7+3);
  const DeviceMetadata device{0xfff1,0x8000,40000,40001,0x1f0000};
  const ImageMetadata valid{0xfff1,0x8000,40001,40000,40000,1502256,true,true,true,digest,32};
  ImagePolicy policy;
  // Real slot and streamed length, varied block boundaries, never allocate a
  // complete firmware in the runtime policy.
  assert(!policy.mayApply()); assert(policy.accept(valid,device));
  size_t remaining=valid.bytes;
  while (remaining) { const size_t block=remaining>1024?1024:remaining; assert(policy.append(block));remaining-=block; }
  assert(!policy.mayApply()); assert(policy.finish(digest,32)); assert(policy.mayApply());
  assert(!policy.append(1)); assert(!policy.mayApply());
  for (unsigned scenario=0;scenario<13;++scenario) {
    policy.reset(); auto image=valid; auto target=device;
    switch(scenario) {
      case 0:image.vendor=0x1234;break;
      case 1:image.product=0x0001;break;
      case 2:image.version=40000;break;
      case 3:image.version=39999;break;
      case 4:image.version=40002;break;
      case 5:image.minimum=40001;break;
      case 6:image.maximum=39999;break;
      case 7:image.bytes=0;break;
      case 8:image.bytes=target.slotBytes+1;break;
      case 9:image.bytes=std::numeric_limits<uint64_t>::max();break;
      case 10:image.sha256=false;break;
      case 11:image.digestBytes=31;break;
      case 12:image.digest=nullptr;break;
    }
    assert(!policy.accept(image,target)); assert(!policy.mayApply());
    // Any failed header remains rejected until the next explicit prepare.
    assert(!policy.accept(valid,device));
  }
  policy.reset(); assert(policy.accept(valid,device)); assert(policy.append(valid.bytes-1));
  assert(!policy.finish(digest,32)); assert(!policy.mayApply()); assert(!policy.append(1));
  for (unsigned byte=0;byte<32;++byte) {
    policy.reset(); assert(policy.accept(valid,device)); assert(policy.append(valid.bytes));
    digest[byte]^=1; assert(!policy.finish(digest,32)); assert(!policy.mayApply()); digest[byte]^=1;
  }
  policy.reset(); assert(policy.accept(valid,device)); assert(!policy.append(valid.bytes+1)); assert(!policy.mayApply());
  policy.reset(); assert(policy.accept(valid,device)); assert(!policy.accept(valid,device)); assert(!policy.mayApply());
  policy.reset(); assert(!policy.append(0)); assert(!policy.finish(digest,32));
  policy.reset(); assert(policy.accept(valid,device)); assert(policy.append(valid.bytes));
  assert(policy.finish(digest,32)); assert(!policy.finish(digest,32)); assert(!policy.mayApply());
  puts("OTA policy: compatibility, length, integrity and failure latch passed");
}
