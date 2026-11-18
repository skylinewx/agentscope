/*
 * @(#)NvwaCerityTokenController.java  
 */
package com.jiuqi.nvwa.certification.web;

import java.io.Serializable;
import java.net.URL;
import java.net.URLDecoder;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.util.StringUtils;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.jiuqi.np.core.application.NpApplication;
import com.jiuqi.np.core.context.NpContext;
import com.jiuqi.np.core.context.NpContextBuilder;
import com.jiuqi.np.core.context.NpContextHolder;
import com.jiuqi.np.core.context.impl.NpContextOrganization;
import com.jiuqi.nvwa.certification.authentication.client.CertificationClientAuthentication;
import com.jiuqi.nvwa.certification.authentication.ticket.CertificationTicketAuthentication;
import com.jiuqi.nvwa.certification.authentication.token.CertificationTokenAuthentication;
import com.jiuqi.nvwa.certification.bean.NvwaApp;
import com.jiuqi.nvwa.certification.bean.NvwaCertify;
import com.jiuqi.nvwa.certification.constant.CeritficationConstant;
import com.jiuqi.nvwa.certification.manage.INvwaAppRequestManage;
import com.jiuqi.nvwa.certification.manage.INvwaCertificationUrlManage;
import com.jiuqi.nvwa.certification.manage.INvwaCertifyAppExtendManager;
import com.jiuqi.nvwa.certification.service.INvwaAppService;
import com.jiuqi.nvwa.certification.service.INvwaCertifyService;
import com.jiuqi.nvwa.certification.util.NvwaCertificationI18nUtil;
import com.jiuqi.nvwa.intergration.sdk.cer.dto.NvwaTicketDTO;
import com.jiuqi.nvwa.intergration.sdk.cer.dto.NvwaTokenDTO;
import com.jiuqi.nvwa.intergration.sdk.cer.dto.ResultObject;
import com.jiuqi.nvwa.intergration.sdk.cer.util.NvwaCerityConstant;

import io.swagger.annotations.Api;

/**
 *<p>Title:</p>
 * @author: lixihao
 * @date: 2021/03/22
 * @version: v1.0
 */
@Api(tags= {"统一认证中心|票据申请、验证"})
@RestController
@RequestMapping(NvwaCerityConstant.TICKET_REST)
public class NvwaCerityTicketController {
	
	private static final Logger LOGGER = LoggerFactory.getLogger(NvwaCerityTicketController.class);

	@Autowired
	private INvwaCertifyAppExtendManager nvwaCertifyExtendManager;
	@Autowired
	private INvwaAppRequestManage nvwaAppRequestManage;
	@Autowired
	private NpApplication npApplication;
//	@Autowired
//	private NvwaTicketClientConfig clientDao;
	@Autowired
	private INvwaAppService nvwaAppService;
	@Autowired
	private INvwaCertificationUrlManage nvwaCertificationUrlManage;
	@Autowired
	private INvwaCertifyService nvwaCertifyService;

	/**
	 * 	为一个应用服务申请一个票据
	 * @param appId app标识
	 * @param loginDate 指定登陆日期  yyyy-MM-dd
	 * @param orgCode	指定登陆组织机构
	 * @param extInfo 指定扩展信息 base64({'key':'value'})
	 * @return
	 */
	@RequestMapping("/apply")
	public ResultObject<NvwaTicketDTO> apply(
			@RequestParam(name="response_type",required=false) String response_type,
			@RequestParam(name="client_id",required=false) String client_id,
			@RequestParam(name="redirect_uri",required=false) String redirect_uri,
			@RequestParam(name="state",required=false) String state,
			@RequestParam(name="appId",required=false) String appId,
			@RequestParam(value="loginDate", required = false) @DateTimeFormat(pattern="yyyy-MM-dd") Date loginDate,
			@RequestParam(value="orgCode", required = false) String orgCode,
			@RequestParam(value="extInfo", required = false) String extInfo,
			@RequestParam(value="details", required = false) boolean details) {
		try {
			NpContextBuilder builder = new NpContextBuilder();
			NpContext context = NpContextHolder.getContext();
			if(null != loginDate || StringUtils.hasLength(orgCode) || StringUtils.hasLength(extInfo)) {
				builder.from(context);
				if(null != loginDate) {
					builder.date(loginDate);
				}
				//登陆组织机构
				if(StringUtils.hasLength(orgCode)) {
					NpContextOrganization npContextOrganization = new NpContextOrganization();
					npContextOrganization.setCode(orgCode);
					builder.organization(npContextOrganization);
				}
				NpContext build = builder.build();
				if(StringUtils.hasLength(extInfo)) {
					extInfo = URLDecoder.decode(extInfo, "UTF-8");
					Base64.Decoder decoder = Base64.getDecoder();
					extInfo = new String(decoder.decode(extInfo), "UTF-8");
					ObjectMapper objectMapper = new ObjectMapper();
					@SuppressWarnings("unchecked")
					HashMap<String,Object> map = objectMapper.readValue(extInfo, HashMap.class);
					map.entrySet().stream().forEach(o -> {
						Object value = o.getValue();
						if(null != value) {
							if(value instanceof Serializable) {
								build.getDefaultExtension().put(o.getKey(),(Serializable)value);
							}
						}else {
							build.getDefaultExtension().put(o.getKey(),null);
						}
					});
				}
				context = build;
			}
			if(StringUtils.hasLength(client_id)) {
				NvwaApp selectByClientid = nvwaAppService.selectByClientid(client_id);
				if(null == selectByClientid || !CeritficationConstant.CURENT_SERVICE_ID.equals(selectByClientid.getCsId())) {
					throw new RuntimeException("当前客户端id未找到对应应用！client_id："+ client_id);
				}
				//检验 回调地址是否是合法的
				if(!StringUtils.hasLength(redirect_uri)) {
					throw new RuntimeException("回调地址不能为空！");
				}
				List<String> urls = new ArrayList<>();
				//查这个认证下所有的应用是，A应用，单点B应用场景 A的客户端id B的回调地址
				List<NvwaApp> apps = nvwaAppService.selectByCsId(selectByClientid.getCsId());
				for (NvwaApp nvwaApp : apps) {
					String reallyFrontendURL = nvwaCertificationUrlManage.getReallyFrontendURL(nvwaApp);
					String reallyUrl = nvwaCertificationUrlManage.getReallyUrl(nvwaApp);
					urls.add(reallyUrl);
					urls.add(reallyFrontendURL);
				}
				NvwaCertify nvwaCertify = nvwaCertifyService.selectById(selectByClientid.getCsId());
				String certifyFrontendURL = nvwaCertificationUrlManage.getReallyFrontendURL(nvwaCertify);
				if(StringUtils.hasLength(certifyFrontendURL)) {
					urls.add(certifyFrontendURL);
				}
				URL callUrl = new URL(redirect_uri);
				boolean safe = false;
	            for (String url : urls) {
	                URL sqlUrl = new URL(url);
	                if(callUrl.getProtocol().equals(sqlUrl.getProtocol()) && callUrl.getHost().equals(sqlUrl.getHost()) && callUrl.getPort() == sqlUrl.getPort()){
	                	safe = true;
	                	break;
	                }
	            }
	            if(!safe) {
	            	LOGGER.error("回调地址域名为："+ redirect_uri);
	            	for (String url : urls) {
	            		LOGGER.error("现有的备案域名为："+ url);
	            	}
	            	throw new RuntimeException("回调地址不是已经备案过的安全域名！client_id："+client_id);
	            }
	            appId = selectByClientid.getId();
			}
			String appIdFromClient = CertificationClientAuthentication.getAppId();
			if(StringUtils.hasLength(appIdFromClient)) {
				appId = appIdFromClient;
			}
			final String app =  appId;
			NvwaTicketDTO applyTicket = npApplication.runAsContext(context, new Callable<NvwaTicketDTO>() {
				@Override
				public NvwaTicketDTO call() throws Exception {
					NvwaTicketDTO applyTicket = nvwaAppRequestManage.applyTicket(app, StringUtils.hasLength(appIdFromClient));
					return applyTicket;
				}
			});	
			//页面单点，记录颁发的ticket
			NvwaApp nvwaApp = null;
			if(StringUtils.hasLength(client_id)) {
				nvwaApp =  nvwaAppService.selectByClientid(client_id);
			} else if (StringUtils.hasLength(appId)) {
				nvwaApp = nvwaAppService.selectById(appId);
				if (nvwaApp == null) {
					nvwaApp = nvwaAppService.selectByCode(appId);
				}
			}
			if (null != nvwaApp) {
				nvwaAppRequestManage.ssoTicketLog(applyTicket.getId(), nvwaApp.getId());
			}
			if(!details) {
				applyTicket.setUser(null);
			} 
			return ResultObject.Ok(applyTicket);
		} catch (Exception e) {
			LOGGER.error("申请票据出错!",e);
			return ResultObject.Error(e.getMessage());
		}
	}
	
	/**
	 * 	校验票据的合法性 同时返回token
	 * @param request
	 * @return
	 */
	@RequestMapping("/verify")
	public ResultObject<NvwaTokenDTO> verifyTicket(@RequestParam(name=CeritficationConstant.CERTIFY_CODE,required=false) String cs,
			@RequestParam(name=CeritficationConstant.TICKET_ID,required=false) String ticketId,
			@RequestParam(value="details", required = false) boolean details) {
		try {
			NvwaTokenDTO token = CertificationTicketAuthentication.getToken();
			if(null != token) {
				if(!details) {
					token.setUser(null);
				}
				return ResultObject.Ok(token);
			}
			NvwaTokenDTO verifyTicket = nvwaCertifyExtendManager.verifyTicket(cs,ticketId);
			if(null != verifyTicket) {
				if(!details) {
					verifyTicket.setUser(null);
				}
				return ResultObject.Ok(verifyTicket);
			}
			Map<String, String> args = new HashMap<>();
			args.put("ticket", "");
			return ResultObject.Error(NvwaCertificationI18nUtil.buildCode("nvwa.certification.checkFailed", args));
		} catch (Exception e) {
			LOGGER.error("验证票据出错!",e);
			return ResultObject.Error(e.getMessage());
		}
	};
	
	
	/**
	 * 	校验Token
	 * @param request
	 * @return
	 */
	@RequestMapping("/token")
	public ResultObject<NvwaTokenDTO> verifyToken(
			@RequestParam(name=CeritficationConstant.CERTIFY_CODE,required=false) String cs,
			@RequestParam(name=CeritficationConstant.TOKEN_ID,required=false) String tokenId,
			@RequestParam(value="details", required = false) boolean details) {
		try {
			NvwaTokenDTO token = CertificationTokenAuthentication.getToken();
			if(null != token) {
				if(!details) {
					token.setUser(null);
				}
				return ResultObject.Ok(token);
			}
			NvwaTokenDTO verifyToken = nvwaCertifyExtendManager.verifyToken(cs, tokenId);
			if(null != verifyToken) {
				if(!details) {
					verifyToken.setUser(null);
				}
				return ResultObject.Ok(verifyToken);
			}
			Map<String, String> args = new HashMap<>();
			args.put("ticket", "");
			return ResultObject.Error(NvwaCertificationI18nUtil.buildCode("nvwa.certification.checkFailed", args));
		} catch (Exception e) {
			LOGGER.error("验证token出错!",e);
			return ResultObject.Error(e.getMessage());
		}
	};
	
}
