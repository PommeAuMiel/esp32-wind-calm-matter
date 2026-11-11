#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <functional>
#include <string>
#include <vector>
#include <algorithm>
using CHIP_ERROR=int;
constexpr CHIP_ERROR CHIP_NO_ERROR=0,CHIP_ERROR_INCORRECT_STATE=1,CHIP_ERROR_INVALID_ARGUMENT=2,
 CHIP_ERROR_BUFFER_TOO_SMALL=3,CHIP_ERROR_INTEGRITY_CHECK_FAILED=4,CHIP_ERROR_INTERNAL=5,CHIP_ERROR_READ_FAILED=6;
#define ReturnErrorOnFailure(expression) do { const auto _error=(expression); if (_error!=CHIP_NO_ERROR) return _error; } while(0)
#define VerifyOrReturnError(condition,error) do { if (!(condition)) return error; } while(0)
constexpr int ESP_OK=0,ESP_IMAGE_VERIFY=0;
struct esp_partition_t { uint32_t address,size; };
struct esp_partition_pos_t { uint32_t address,size; };
struct esp_image_metadata_t {};
struct esp_app_desc_t { char version[32]{},project_name[32]{}; };
namespace mock {
inline int beginError=0,addError=0,finishError=0,nativePrepareError=0,nativeProcessError=0,
 nativeFinalizeError=0,nativeApplyError=0,nativeAbortError=0,vendorError=0,productError=0,
 versionError=0,parserError=0,queueError=0,imageError=0,descriptionError=0,readError=0;
inline unsigned prepareCalls=0,processCalls=0,finalizeCalls=0,applyCalls=0,abortCalls=0,cancelCalls=0;
inline bool noSlot=false,runningSlot=false,noRequestor=false,clearBackingOnClear=false;
inline unsigned readCalls=0;
inline esp_partition_t running{0x10000,0x1f0000},inactive{0x200000,0x1f0000};
inline esp_app_desc_t description{};
inline std::vector<uint8_t> nativeBytes;
inline std::vector<uint8_t> flashBytes{1,2,3};
inline std::vector<std::function<void()>> queue;
inline uint8_t expectedDigest[32]{};
inline std::string versionString="4.0.1-thread-ota";
inline void reset() {
 beginError=addError=finishError=nativePrepareError=nativeProcessError=nativeFinalizeError=nativeApplyError=nativeAbortError=0;
 vendorError=productError=versionError=parserError=queueError=imageError=descriptionError=readError=0;
 prepareCalls=processCalls=finalizeCalls=applyCalls=abortCalls=cancelCalls=0;
 noSlot=runningSlot=noRequestor=clearBackingOnClear=false;readCalls=0; nativeBytes.clear();queue.clear();flashBytes={1,2,3};
 memset(expectedDigest,0,32);expectedDigest[0]=1;expectedDigest[1]=2;expectedDigest[2]=3;
 versionString="4.0.1-thread-ota";
 strcpy(description.version,versionString.c_str());strcpy(description.project_name,"windcalm-thread-ota");
 }
}
inline const esp_partition_t *esp_ota_get_running_partition(){return &mock::running;}
inline const esp_partition_t *esp_ota_get_next_update_partition(const void*) {
 return mock::noSlot?nullptr:(mock::runningSlot?&mock::running:&mock::inactive);
}
inline int esp_partition_read(const esp_partition_t*,size_t offset,void *out,size_t bytes){
 ++mock::readCalls;
 if(mock::readError)return mock::readError;
 if(offset+bytes>mock::flashBytes.size())return CHIP_ERROR_READ_FAILED;
 memcpy(out,mock::flashBytes.data()+offset,bytes);return ESP_OK;
}
inline int esp_image_verify(int,const esp_partition_pos_t*,esp_image_metadata_t*){return mock::imageError;}
inline int esp_ota_get_partition_description(const esp_partition_t*,esp_app_desc_t *output){*output=mock::description;return mock::descriptionError;}
namespace chip {
class ByteSpan {
 public: ByteSpan():pointer(nullptr),length(0){} ByteSpan(const uint8_t*p,size_t n):pointer(p),length(n){}
 template<size_t N>ByteSpan(const uint8_t(&p)[N]):ByteSpan(p,N){}
 const uint8_t*data()const{return pointer;}size_t size()const{return length;}
 ByteSpan SubSpan(size_t n)const{return ByteSpan(pointer+n,length-n);}
 private:const uint8_t*pointer;size_t length;
};
class MutableByteSpan {
 public:template<size_t N>MutableByteSpan(uint8_t(&p)[N]):pointer(p),length(N){}
 uint8_t*data(){return pointer;}size_t size()const{return length;}
 private:uint8_t*pointer;size_t length;
};
class CharSpan {
 public: CharSpan()=default; CharSpan(const char*p,size_t n):pointer(p),length(n){}
 const char*data()const{return pointer;}size_t size()const{return length;}
 private:const char*pointer=nullptr;size_t length=0;
};
template<typename T>class Optional {
 public:Optional()=default;Optional(T v):present(true),value(v){}
 bool HasValue()const{return present;}T ValueOr(T fallback)const{return present?value:fallback;}
 private:bool present=false;T value{};
};
namespace app { namespace DataModel {
template<typename T>class Nullable {
 public:Nullable()=default;Nullable(T v):present(true),value(v){}
 bool IsNull()const{return !present;}T Value()const{return value;}
 private:bool present=false;T value{};
};
}}
struct OTAImageProgress{uint64_t downloadedBytes=0,totalFileBytes=0;};
class OTAImageProcessorInterface {
 public:virtual ~OTAImageProcessorInterface()=default;
 virtual CHIP_ERROR PrepareDownload()=0;virtual CHIP_ERROR ProcessBlock(ByteSpan&)=0;
 virtual CHIP_ERROR Finalize()=0;virtual CHIP_ERROR Apply()=0;virtual CHIP_ERROR Abort()=0;
 virtual bool IsFirstImageRun()=0;virtual CHIP_ERROR ConfirmCurrentImage()=0;
 virtual app::DataModel::Nullable<uint8_t>GetPercentComplete(){
  return mParams.totalFileBytes?app::DataModel::Nullable<uint8_t>(uint8_t(mParams.downloadedBytes*100/mParams.totalFileBytes)):app::DataModel::Nullable<uint8_t>();
 }
 virtual uint64_t GetBytesDownloaded(){return mParams.downloadedBytes;}
 protected:OTAImageProgress mParams;
};
enum class OTAImageDigestType:uint8_t{kSha256=1,kSha256_128=2};
struct OTAImageHeader {
 uint16_t mVendorId=0xfff1,mProductId=0x8000;uint32_t mSoftwareVersion=40001;
 CharSpan mSoftwareVersionString;uint64_t mPayloadSize=3;Optional<uint32_t>mMinApplicableVersion{40000},mMaxApplicableVersion;
 OTAImageDigestType mImageDigestType=OTAImageDigestType::kSha256;ByteSpan mImageDigest;
};
namespace fake {inline OTAImageHeader image;}
class OTAImageHeaderParser {
 public:void Init(){active=true;used=0;}void Clear(){
  if(active && used==8 && mock::clearBackingOnClear){memset(mock::expectedDigest,0,32);mock::versionString.assign(32,'X');}
  active=false;used=0;
 }bool IsInitialized()const{return active;}
 CHIP_ERROR AccumulateAndDecode(ByteSpan&block,OTAImageHeader&output){
  if(mock::parserError)return mock::parserError;
  const size_t consume=std::min(size_t(8)-used,block.size());used+=consume;block=block.SubSpan(consume);
  if(used<8)return CHIP_ERROR_BUFFER_TOO_SMALL;
  output=fake::image;output.mSoftwareVersionString=CharSpan(mock::versionString.c_str(),mock::versionString.size());
  output.mImageDigest=ByteSpan(mock::expectedDigest,32);return CHIP_NO_ERROR;
 }
 private:bool active=false;size_t used=0;
};
namespace Crypto {
constexpr size_t kSHA256_Hash_Length=32;
// SDK boundary fake for injected failures and streaming semantics. The actual
// built Matter payload SHA-256 is checked independently with Python hashlib.
class Hash_SHA256_stream {
 public:CHIP_ERROR Begin(){memset(bytes,0,32);offset=0;return mock::beginError;}
 CHIP_ERROR AddData(ByteSpan block){if(mock::addError)return mock::addError;
  for(size_t i=0;i<block.size();++i)bytes[(offset++)%32]^=block.data()[i];return CHIP_NO_ERROR;}
 CHIP_ERROR Finish(MutableByteSpan&span){if(mock::finishError)return mock::finishError;memcpy(span.data(),bytes,32);return CHIP_NO_ERROR;}
 private:uint8_t bytes[32]{};size_t offset=0;
};
}
namespace DeviceLayer {
class InstanceInfo {public:CHIP_ERROR GetVendorId(uint16_t&v){v=0xfff1;return mock::vendorError;}
 CHIP_ERROR GetProductId(uint16_t&v){v=0x8000;return mock::productError;}};
class Configuration {public:CHIP_ERROR GetSoftwareVersion(uint32_t&v){v=40000;return mock::versionError;}};
inline InstanceInfo info;inline Configuration config;
inline InstanceInfo*GetDeviceInstanceInfoProvider(){return &info;}inline Configuration&ConfigurationMgr(){return config;}
class Platform {public:CHIP_ERROR ScheduleWork(void(*callback)(intptr_t),intptr_t arg){
 if(mock::queueError)return mock::queueError;mock::queue.push_back([callback,arg]{callback(arg);});return CHIP_NO_ERROR;}};
inline Platform platform;inline Platform&PlatformMgr(){return platform;}
}
class OTARequestorInterface {
 public:uint32_t GetTargetVersion(){return 40001;}
 void CancelImageUpdate(){++mock::cancelCalls;}
};
inline OTARequestorInterface requestor;
inline OTARequestorInterface*GetRequestorInstance(){return mock::noRequestor?nullptr:&requestor;}
class OTAImageProcessorImpl:public OTAImageProcessorInterface {
 public:CHIP_ERROR PrepareDownload()override{++mock::prepareCalls;return mock::nativePrepareError;}
 CHIP_ERROR ProcessBlock(ByteSpan&block)override{++mock::processCalls;mock::nativeBytes.insert(mock::nativeBytes.end(),block.data(),block.data()+block.size());return mock::nativeProcessError;}
 CHIP_ERROR Finalize()override{++mock::finalizeCalls;return mock::nativeFinalizeError;}
 CHIP_ERROR Apply()override{++mock::applyCalls;return mock::nativeApplyError;}
 CHIP_ERROR Abort()override{++mock::abortCalls;return mock::nativeAbortError;}
 bool IsFirstImageRun()override{return false;}CHIP_ERROR ConfirmCurrentImage()override{return CHIP_NO_ERROR;}
 app::DataModel::Nullable<uint8_t>GetPercentComplete()override{return app::DataModel::Nullable<uint8_t>(99);}
 uint64_t GetBytesDownloaded()override{return 98765;}
};
}
