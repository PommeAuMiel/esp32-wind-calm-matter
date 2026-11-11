#include "../main/VerifiedOta.h"
#include <cassert>
#include <cstdio>
chip::OTAImageProcessorImpl gImageProcessor;
namespace windcalm_ota { void matterVersionConfirmed() {} }
using windcalm_ota::VerifiedOta;
const uint8_t image[]{10,20,30,40,50,60,70,80,1,2,3};
void defaults() { mock::reset(); chip::fake::image=chip::OTAImageHeader{}; }
void flushQueue() { auto queue=mock::queue;mock::queue.clear();for(auto &job:queue)job(); }
CHIP_ERROR feed(VerifiedOta &ota) { chip::ByteSpan bytes(image);return ota.ProcessBlock(bytes); }
void successToFinalize(VerifiedOta &ota) {
 assert(ota.PrepareDownload()==CHIP_NO_ERROR);assert(feed(ota)==CHIP_NO_ERROR);assert(ota.Finalize()==CHIP_NO_ERROR);
}
void latched(VerifiedOta &ota) {
 const unsigned applies=mock::applyCalls,processes=mock::processCalls,finalizes=mock::finalizeCalls;
 // Clear injected boundary faults: a previous error must remain decisive.
 mock::beginError=mock::addError=mock::finishError=mock::nativePrepareError=mock::nativeProcessError=0;
 mock::nativeFinalizeError=mock::nativeApplyError=mock::vendorError=mock::productError=mock::versionError=0;
 mock::parserError=mock::imageError=mock::descriptionError=0;
 assert(feed(ota)!=CHIP_NO_ERROR);assert(ota.Finalize()!=CHIP_NO_ERROR);assert(ota.Apply()!=CHIP_NO_ERROR);
 assert(mock::applyCalls==applies && mock::processCalls==processes && mock::finalizeCalls==finalizes);
}
int main() {
 { defaults(); VerifiedOta ota;
   ota.setPrepareAllowed([]() { return false; });
   assert(ota.PrepareDownload() == CHIP_ERROR_INCORRECT_STATE);
   latched(ota); flushQueue(); assert(mock::cancelCalls == 1);
   // A cancelled maintenance request can release the gate for a fresh attempt.
   ota.setPrepareAllowed([]() { return true; });
   successToFinalize(ota); assert(ota.Apply() == CHIP_NO_ERROR);
 }
 { defaults();VerifiedOta ota;assert(ota.PrepareDownload()==0);
   chip::ByteSpan first(image,3),second(image+3,5),third(image+8,3);
   assert(ota.ProcessBlock(first)==0);assert(ota.GetPercentComplete().IsNull());
   assert(ota.ProcessBlock(second)==0);assert(ota.GetBytesDownloaded()==0);assert(ota.GetPercentComplete().Value()==0);
   assert(ota.ProcessBlock(third)==0);assert(ota.GetBytesDownloaded()==3);assert(ota.GetPercentComplete().Value()==100);
   assert(mock::nativeBytes==std::vector<uint8_t>(image,image+sizeof(image)));
   assert(ota.Finalize()==0);assert(ota.Apply()==0);assert(mock::applyCalls==1);
   assert(ota.Abort()==0);assert(ota.GetBytesDownloaded()==0);assert(ota.GetPercentComplete().IsNull());
 }
 // Inject errors at every native/crypto/identity boundary actually called by
 // VerifiedOta, not a copy of its policy. Every later Apply must remain blocked.
 for(unsigned fault=0;fault<23;++fault) {
  defaults();VerifiedOta ota;int *failure=nullptr;
  switch(fault) {
   case 0:failure=&mock::beginError;break;case 1:failure=&mock::nativePrepareError;break;
   case 2:failure=&mock::parserError;break;case 3:failure=&mock::vendorError;break;
   case 4:failure=&mock::productError;break;case 5:failure=&mock::versionError;break;
   case 6:mock::noSlot=true;break;case 7:mock::runningSlot=true;break;case 8:mock::noRequestor=true;break;
   case 9:mock::versionString=std::string(32,'v');break;case 10:failure=&mock::addError;break;
   case 11:failure=&mock::nativeProcessError;break;case 12:failure=&mock::finishError;break;
   case 13:failure=&mock::nativeFinalizeError;break;case 14:failure=&mock::imageError;break;
   case 15:failure=&mock::descriptionError;break;case 16:strcpy(mock::description.project_name,"light-app");break;
   case 17:failure=&mock::nativeApplyError;break;
   case 18:failure=&mock::beginError;break;case 19:failure=&mock::addError;break;
   case 20:failure=&mock::finishError;break;case 21:failure=&mock::readError;break;
   case 22:mock::flashBytes[2]^=1;break;
  }
  if(fault<=1) { *failure=CHIP_ERROR_INTERNAL;assert(ota.PrepareDownload()!=0); }
  else if(fault<=11) { assert(ota.PrepareDownload()==0);if(failure)*failure=CHIP_ERROR_INTERNAL;assert(feed(ota)!=0); }
  else if(fault<=13) {assert(ota.PrepareDownload()==0);assert(feed(ota)==0);*failure=CHIP_ERROR_INTERNAL;assert(ota.Finalize()!=0);}
  else {successToFinalize(ota);if(failure)*failure=CHIP_ERROR_INTERNAL;assert(ota.Apply()!=0);}
  latched(ota);flushQueue();assert(mock::cancelCalls==(fault==8?0u:1u));
 }
 {defaults();VerifiedOta ota;assert(ota.PrepareDownload()==0);mock::expectedDigest[0]^=1;
  assert(feed(ota)==0);assert(ota.Finalize()!=0);assert(mock::finalizeCalls==0);latched(ota);}
 {defaults();chip::fake::image.mPayloadSize=4;VerifiedOta ota;assert(ota.PrepareDownload()==0);
  assert(feed(ota)==0);assert(ota.Finalize()!=0);latched(ota);}
 {defaults();chip::fake::image.mPayloadSize=2;VerifiedOta ota;assert(ota.PrepareDownload()==0);
  assert(feed(ota)!=0);latched(ota);}
 {defaults();VerifiedOta ota;assert(ota.PrepareDownload()==0);mock::addError=CHIP_ERROR_INTERNAL;
  assert(feed(ota)!=0);assert(!mock::queue.empty());latched(ota);
  // Abort releases resources, then a complete new attempt can succeed.
  assert(ota.Abort()==0);defaults();successToFinalize(ota);assert(ota.Apply()==0);
 }
 {defaults();VerifiedOta ota;assert(ota.PrepareDownload()==0);mock::addError=CHIP_ERROR_INTERNAL;
  assert(feed(ota)!=0);mock::addError=0;
  // A stale scheduled cancel from an old failure cannot cancel a new prepare.
  assert(ota.PrepareDownload()==0);flushQueue();assert(mock::cancelCalls==0);
  assert(feed(ota)==0);assert(ota.Finalize()==0);assert(ota.Apply()==0);
 }
 {defaults();VerifiedOta ota;assert(ota.PrepareDownload()==0);mock::queueError=CHIP_ERROR_INTERNAL;
  mock::addError=CHIP_ERROR_INTERNAL;assert(feed(ota)!=0);latched(ota);assert(mock::queue.empty());}
 {defaults();VerifiedOta ota;successToFinalize(ota);mock::nativeAbortError=CHIP_ERROR_INTERNAL;
  assert(ota.Abort()!=0);latched(ota);}
 {defaults();mock::clearBackingOnClear=true;VerifiedOta ota;
  successToFinalize(ota);assert(ota.Apply()==0); // Header backing was invalidated by parser.Clear.
 }
 {defaults();VerifiedOta ota;chip::fake::image.mPayloadSize=1025;
  std::vector<uint8_t>large(image,image+8);mock::flashBytes.clear();memset(mock::expectedDigest,0,32);
  for(size_t i=0;i<1025;++i){uint8_t b=uint8_t(i+1);large.push_back(b);mock::flashBytes.push_back(b);mock::expectedDigest[i%32]^=b;}
  assert(ota.PrepareDownload()==0);chip::ByteSpan block(large.data(),large.size());assert(ota.ProcessBlock(block)==0);
  assert(ota.Finalize()==0);assert(ota.Apply()==0);assert(mock::readCalls==3);
 }
 puts("Actual VerifiedOta: fragmented header, corruption/length, all boundary failures, latch and recovery passed");
}
