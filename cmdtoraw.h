
std::vector<long int> cmdtoraw(std::string cmdin) {
                constexpr float br_npulse = 30.45353165; /* = 1/(269/8192) 0.032836914 */ 
                int i,l,p,k,n,slop;
                std::string hexout;
                std::vector<long int> mycodes;
                const std::string b64map="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
                const std::string hexmap="0123456789ABCDEF";
                char chtmp;
                ESP_LOGD("cmdtoraw", "Command string (30 chars): %s",cmdin.substr(0,30).c_str());
                if ((cmdin.substr(0,1)=="[") && (cmdin.substr(cmdin.size()-1,1)=="]"))
                {
                  k=0;cmdin[cmdin.size()-1]=',';
                  p=cmdin.find(',',k);
                  while ((p>0)) {
                      mycodes.push_back(stoi(cmdin.substr(k+1,p-k-1)));
                     /* std::cout<<mycodes.at(mycodes.size()-1);std::cout<<','; */
                  k=p;p=cmdin.find(',',k+1);
                  }
                } else
                {
                /* ------- End of Raw --------------- */
                l=cmdin.length();k=0;
                for (i=0;i<l;i++)
                {
                chtmp=cmdin[i]; 
                if (chtmp=='=') break;
                p=b64map.find(chtmp);
                if (p>-1)
                      {
                        switch(k)
                          {
                              case 0: hexout=hexout+hexmap[int(p/4)];k=1;slop=(p & 3);break;
                              case 1: hexout=hexout+hexmap[(slop*4) | int(p/16)];k=2;slop=(p & 15);break;
                              case 2: hexout=hexout+hexmap[slop]+hexmap[int(p/4)];k=3;slop=(p & 3);break;
                              default:hexout=hexout+hexmap[(slop*4) | int(p/16)]+hexmap[p & 15];k=0;
                          }
                      }
                }
              if (k == 1) hexout=hexout + hexmap[ (slop * 4) ];
              
              ESP_LOGD("cmdtoraw", "Hex string: %s",hexout.substr(0,30).c_str()); 
              
              /* --------- End of C64 to Hex -------- */                        
              l=(stoi(hexout.substr(4,2),0,16)+stoi(hexout.substr(6,2),0,16)*256)*2+8;
              if (!(l>hexout.length()))  
                {
                if (((hexout.substr(0,2)=="26") && (hexout.substr(l-4,4)=="0D05"))) 
                  {
                    for (n=8;n<l-6;n+=2)
                    {
                      k=stoi(hexout.substr(n,2),nullptr,16);
                      if (k==0) {k=stoi(hexout.substr(n+2,4),nullptr,16); n+=4;}
                      k=int(k*br_npulse); if ( (mycodes.size() % 2) != 0)  k=-k;
                      mycodes.push_back(k);
                      
                    }
                  }
                } 
              }
              return mycodes;
              }
