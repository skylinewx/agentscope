package com.jiuqi.nvwa.certification.authentication.ticket;

import java.nio.charset.StandardCharsets;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

import com.jiuqi.np.user.SystemUserDTO;
import com.jiuqi.np.user.feign.client.NvwaSystemUserClient;
import com.jiuqi.nvwa.certification.authentication.CertificationShiroTokenHolder;
import com.jiuqi.nvwa.certification.bean.NvwaCertify;
import com.jiuqi.nvwa.certification.exception.CertifyRuntimeException;
import com.jiuqi.nvwa.certification.manage.INvwaCertifyAppExtendManager;
import com.jiuqi.nvwa.certification.service.INvwaCertifyService;
import com.jiuqi.nvwa.certification.util.NvwaCertificationLogUtil;
import com.jiuqi.nvwa.intergration.sdk.cer.dto.NvwaTokenDTO;
import com.jiuqi.nvwa.intergration.sdk.cer.dto.NvwaUserDTO;
import com.jiuqi.nvwa.intergration.sdk.cer.util.NvwaCerityConstant;
import com.jiuqi.nvwa.login.constant.LoginConsts;
import com.jiuqi.nvwa.springadapter.servlet.NvwaRequstBase;
import com.jiuqi.nvwa.springadapter.shiro.INvwaShiroAuthenticationSpringAdaptor;
import com.jiuqi.nvwa.ticket.authentication.AuthenticationConstants;

/**
 * 我是应用服务的时候 去其他服务器认证 我是认证服务的时候,本地认证
 * 
 * 其他服务器 带着 认证服务颁发的 ticket 想要获取当前服务的资源
 * 1、在Header上传递认证中心颁发的ticket信息。示例：authorization-cer-ticket:
 * base64(remoteCode:ticket) 2、值是 remoteCode 和 请求
 * /nvwa-certification/v1/ticket/apply 返回的 ticket id base64编码
 * 3、会根据认证服务code找到对应认证中心去验证 ticket 是否是合法的ticket
 */
@Order(-1)
@Component
public class CertificationTicketAuthentication implements INvwaShiroAuthenticationSpringAdaptor {

	@Autowired
	private CertificationShiroTokenHolder certificationShiroTokenHolder;
	@Autowired
	private INvwaCertifyService nvwaCertifyService;
	@Autowired
	private INvwaCertifyAppExtendManager nvwaCertifyExtendManager;
	@Autowired
	private NvwaSystemUserClient systemUserService;
	

	private static final Logger LOGGER = LoggerFactory.getLogger(CertificationTicketAuthentication.class);

	@Override
	public String getTokenId(NvwaRequstBase request) {
		String authorization = request.getHeader(NvwaCerityConstant.TICKET_HEADER_NAME);
		if (!StringUtils.hasLength(authorization)) {
			authorization = request.getParameter(NvwaCerityConstant.TICKET_HEADER_NAME);
		}
		if (!StringUtils.hasLength(authorization)) {
			return AuthenticationConstants.UNKNOW_TOKEN_ID;
		}
		TicketDecoder decoder = new TicketDecoder(authorization, StandardCharsets.UTF_8);
		if (!decoder.tryDecode()) {
			String errorMessage = "Ticket认证失败[" + authorization + "]加密格式不符合规范!";
			LOGGER.error(errorMessage);
			NvwaCertificationLogUtil.tryCerInfoError("Ticket认证", errorMessage);
			throw new CertifyRuntimeException(errorMessage);
		}
		if(!StringUtils.hasLength(decoder.getRemoteCode())) {
			decoder.setRemoteCode(nvwaCertifyService.selectCurentService().getCode());
		}
		NvwaCertify nvwaCertify = nvwaCertifyService.selectByCode(decoder.getRemoteCode());
		if (null == nvwaCertify) {
			String errorMessage = "Ticket认证失败[" + authorization + "],认证服务未找到[" + decoder.getRemoteCode() + "]";
			LOGGER.error(errorMessage);
			NvwaCertificationLogUtil.tryCerInfoError("Ticket认证", errorMessage);
			throw new CertifyRuntimeException(errorMessage);
		}
		// 去认证中心去验证 tikcet
		NvwaTokenDTO nvwaVerifyTokenDTO = nvwaCertifyExtendManager.verifyTicket(decoder.getRemoteCode(),
					decoder.getTicket());
		if (null != nvwaVerifyTokenDTO) {
			TokenRegister.put(nvwaVerifyTokenDTO);
			if(null != nvwaVerifyTokenDTO.getIsToken() && nvwaVerifyTokenDTO.getIsToken()) {
				return nvwaVerifyTokenDTO.getId(); 
			}
			String tokenId = null;
			try {
				NvwaUserDTO user = nvwaVerifyTokenDTO.getUser();
				user.addExt(LoginConsts.LOGIN_TYPE, "Ticket认证");
				tokenId = certificationShiroTokenHolder.get(decoder.getRemoteCode(), user,true);
			} catch (Exception e) {
				NvwaCertificationLogUtil.tryCerInfoError("Ticket认证", "未知异常[" + e.getMessage() + "]");
				throw e;
			}
			SystemUserDTO systemUserDTO = systemUserService.findByUsername(nvwaVerifyTokenDTO.getUser().getName());
			int userType = null != systemUserDTO ?  systemUserDTO.getUserType() : 0;
			NvwaCertificationLogUtil.tryCerInfoSuccess("Ticket认证", "认证成功",nvwaVerifyTokenDTO.getUser().getName(), userType);
			return tokenId;
		} else {
			String errorMessage = "Ticket认证失败[" + authorization + "],认证服务[" + decoder.getRemoteCode() + "]Ticket校验失败";
			LOGGER.error(errorMessage);
			NvwaCertificationLogUtil.tryCerInfoError("Ticket认证", errorMessage);
			throw new CertifyRuntimeException(errorMessage);
		}
	}
	
	private static class TokenRegister {

		private static final ThreadLocal<NvwaTokenDTO> TokenHolder = new ThreadLocal<>();

		public static void put(NvwaTokenDTO nvwaTokenDTO) {
			TokenHolder.set(nvwaTokenDTO);
		}

		public static NvwaTokenDTO get() {
			return TokenHolder.get();
		}

		public static void clear() {
			TokenHolder.remove();
		}

	}

	/**
	 * 如果是ticket认证可以从这里获取到 token
     */
	public static NvwaTokenDTO getToken() {
		NvwaTokenDTO appId = TokenRegister.get();
		TokenRegister.clear();
		return appId;
	}

}
