from django.conf import settings
from django import forms
from django.utils.safestring import mark_safe
from .forms import _field_for_db
from .models import AnswerText

class NoteWidget(forms.Widget):
	"""
	A NoteField Widget
	"""
	def __init__(self, attrs={}):
		super(NoteWidget, self).__init__(attrs)
	
	def render(self, name, value, attrs=None):
		return ""
	
class FileWidget(forms.FileInput):
	"""
	A FileField Widget that shows its current value if it has one.
	"""
	def __init__(self, attrs={}):
		super(FileWidget, self).__init__(attrs)

	def render(self, name, value, attrs=None):
		output = []
		output.append(super(FileWidget, self).render(name, value, attrs))
		if value:
			# FIXME:  Possible figure out a way to dynamically pass widget arguments to create the proper file path.
			#if hasattr(value, "name"):
			#	value = "/".join([date.today().strftime(settings.UPLOAD_PATH), value.name])
			
			output.append('<div class="cfile">%s <a target="_blank" href="%s">%s</a></div>' % \
				('Currently:', '/'.join([settings.MEDIA_URL, value]), value))
		return mark_safe(u''.join(output))
	
class AjaxSingleFileWidget(forms.TextInput):
	"""
	A file upload widget which handles a single file and uploads it
	by 
	"""

	def __init__(self, attrs={}):
		super(AjaxSingleFileWidget, self).__init__(attrs)

	def render(self, name, value, attrs=None):
		output = []
		
		# break up name to be DB readable
		field_name = _field_for_db(name)
		# query all answertexts for this field & submission
		answers = AnswerText.objects.filter(answer__field__slug=field_name, answer__answertext__text=value).distinct()
		
		files = ''
		answer_ids = []
		if answers:
			for answer in answers:
				answer_ids.append(answer.id)
				value = answer.text
				full_path = ''.join([settings.MEDIA_URL, value])
				files += """<li>
							<a class="del_upload" id="%s" name="%s" href="" style="color:red;">X</a>
							<a href="%s" target="_blank">%s</a>
							</li>""" % (answer.id, value, full_path, value.split("/")[-1])
			
		vals = {
			'name' : name,
			'files' : files,
			'answer_ids' : answer_ids
		}
		
		output.append("""
			<div id="button_%(name)s" class="uploadFile">Upload</div>
			<ul class="files">
				%(files)s
			</ul>
			<input id="id_%(name)s" type="hidden" name="%(name)s" value="" />
			
			<script type="text/javascript">
				$(function() {
					del_upload();
					var button = $('#button_%(name)s');
					var interval;
					
					var upload_url = '/upload/';
					if (typeof(get_upload_url) != "undefined") {
						upload_url = get_upload_url();
					}
					
					new AjaxUpload(button, {
						action: upload_url,
						name: button.attr("id"),
						onSubmit: function(file, ext){
							button.text('Uploading');
													
							// Disable upload button to allow uploading only 1 file at time
							this.disable();
							
							// Uploading. -> Uploading.. -> Uploading...
							interval = window.setInterval(function(){
								var text = button.text();
								if (text.length < 13){
									button.text(text + '.');					
								} else {
									button.text('Uploading');				
								}
							}, 200);
						},
						onComplete: function(file, response){
							button.text('Upload');
							
							window.clearInterval(interval);
							
							// Add the path to the hidden variable input in order to save
							button.next(".files").next("input").val(response);
							
							// Save the collection on each upload for history & to make multiple uploads work
							saveCollection();
							
							// Enable upload button
							this.enable();
							
							var link = response.split("/");
							var upload_path = {'path': response.slice(8)}
							var request = getFileUploadAnswerTextID(upload_path);
							
							request.done(function(data) {
								var upload_id = data;
								
								link = '<a class="del_upload" id="'+upload_id+'" name="'+response.slice(8)+'" href="" style="color:red;">X</a>\
								<a href="'+response+'" target="_blank">'+link[link.length-1]+'</a>';
								
								// add file to the list
								button.next(".files").prepend($('<li></li>').html(link));
								
								$("#" + data).click(function(e) {
									e.preventDefault();
									var del_file = confirm("You are about to delete a file, are you sure you want to do this?");
									var del_path = {'path': $(this).attr("name")};
									if (del_file) {
										deleteFile(del_path); // Call delete JS function
										$(this).parent().remove(); // Remove file from the list
									}
								});
								
								// Remove the input value to avoid submitting duplicate answer_text for submissions on section changes
								button.next(".files").next("input").val(null);
							})
						}
					});
					
					function del_upload() {
						// Function to handle confirmation box, calling of delete function, and 
						// ajax removal of file from list
						for (var id in %(answer_ids)s) {
							var answer_object = $(".files").find("#" + %(answer_ids)s[id]);
							$(answer_object).click(function(e) {
								e.preventDefault();
								var del_file = confirm("You are about to delete a file, are you sure you want to do this?");
								var del_path = {'path': $(this).attr("name")};
								if (del_file) {
									deleteFile(del_path); // Call delete JS function
									$(this).parent().remove(); // Remove file from the list
								}
						
							});
						}
					}
				});
			</script>
		
		""" % vals)
		
		return mark_safe(u''.join(output))