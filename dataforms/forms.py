"""
Dataforms System
================

See the GettingStarted guide at:
http://code.google.com/p/django-dataforms/wiki/GettingStarted
"""
from collections import defaultdict
from django import forms
from django.conf import settings
from django.forms.forms import BoundField
from django.template.defaultfilters import safe, force_escape
from django.utils import simplejson as json
from django.utils.datastructures import SortedDict
from django.utils.safestring import mark_safe
from models import DataForm, Collection, Field, FieldChoice, Choice, Answer, \
    Submission, CollectionDataForm, Section, Binding
from utils.file_handler import handle_upload
from utils.sql import update_many, insert_many, delete_many

from settings import FIELD_MAPPINGS, SINGLE_CHOICE_FIELDS, MULTI_CHOICE_FIELDS, \
    CHOICE_FIELDS, UPLOAD_FIELDS, FIELD_DELIMITER, STATIC_CHOICE_FIELDS, \
    FORM_MEDIA, VALIDATION_MODULE
import datetime
    

# Load the user's custom validation, if it exists
try: validation = __import__(VALIDATION_MODULE, fromlist=['*'])
except ImportError: validation = None


class BaseDataForm(forms.BaseForm):
    
    def __init__(self, *args, **kwargs):
        super(BaseDataForm, self).__init__(*args, **kwargs)
        self._generate_bound_fields()

 
    def __getattr__(self, name):
        if 'clean_' in name:
            # Remove the form-name__ from clean_form-name__textbox
            # and make all dashes underscores (so that clean_some_field_slug will be called)
            validation_func_name = name.replace("".join([self.slug, FIELD_DELIMITER]), "").replace("-", "_")
            
            # Have to use dir() here instead of hasattr because hasattr calls getattr and catches exceptions :)
            if validation_func_name not in dir(self):
                raise AttributeError(validation_func_name)
            return getattr(self, validation_func_name)
        else:
            raise AttributeError("%s doesn't exist in %s" % (name, repr(self)))
    
    
    
    def __iter__(self):
        """
        Overload of the BaseForm iteration to maintain a persistent set of bound_fields.
        This allows us to inject attributes in to the fields and these attributes will
        persist the next time that the form is iterated over.
        """
        
        for name in self.bound_fields:
            yield self.bound_fields[name]
    

    def is_valid(self, check_required=True, process_full_form=True):
        """
        :arg check_required: Whether or not to validate required fields. Default True.
        :arg process_full_form: If True, all fields in the form POST will be handled normally
            (i.e., unchecked checkboxes will not appear in the form POST and so any
            previously checked answer will be deleted). If False, only fields specified
            in the FORM post will be handled (allowing changes to a subset of field answers
            in the database, if desired, but causes checkboxes to not become unchecked unless
            the key is manually added to the form POST data with a blank string value).

            This function *will* affect what the save() function receives to process and
            MUST be called before save() is called.
        """
        
        self._remove_extraneous_fields(process_full_form=process_full_form)
        
        if not check_required:
            for field in self:
                field.field.required = False
            
        return super(BaseDataForm, self).is_valid()
    
    
    def save(self, collection=None):
        """    
        Saves the validated, cleaned form data. If a submission already exists,
        the new data will be merged over the old data.
        """
        
        # TODO: think about adding an "overwrite" argument to this function, default of False,
        # which will determine if an error should be thrown if the submission object already
        # exists, or if we should trust the data and overwrite the previous submission.

        # If there is no submission object, then this should just be a normal
        # Django form.  No need to call the save method, so we will raise an exception
        if not hasattr(self, 'submission'):
            raise LookupError("There is no submission object.  Are your creating the form with 'retrun_class=True'? If so, no need to call save.")

        if not self.cleaned_data:
            raise LookupError("The is_valid() method must be called before saving a form")
        
        # Slightly evil, do type checking to see if submission is a Submission object or string
        # If Submission object is a slug
        if isinstance(self.submission, str) or isinstance(self.submission, unicode):
            submission_slug = self.submission
            
            # Get or create the object
            self.submission, was_created = Submission.objects.get_or_create(slug=submission_slug, collection=collection)
            
        # Otherwise it should be a submission model object, if not raise
        elif not isinstance(self.submission, Submission):
            raise AttributeError('Submission %s is not a valid submission object.' % self.submission)
        

        # We now have a submission object, so let's update the last_modified field
        self.submission.last_modified = datetime.datetime.now()
        self.submission.save()
        
        # Delete Answers if they exist and start over ;)
        Answer.objects.select_related(
            'submission',
            'data_from',
            'field').filter(
                        data_form__slug=self.slug,
                        submission=self.submission).delete()
        
        # If answers don't exist, create the records without answers so that 
        # we can populate them later and save the relationships 
        # This is more efficient then using the ORM
        #if not answers:
            
        field_keys = []
        answers = []
        for key in self.fields.keys():
            # Mangle the key into the DB form, then get the right Field
            field_keys.append(_field_for_db(key))
        
        # Get All Fields
        fields = self.query_data['fields_list']
        
        for field in fields:
            # save the answer only if the field is in the form POST
            if field['slug'] in field_keys:
                # Create a new answer object
                answer = Answer()
                answer.submission = self.submission
                answer.data_form = self.query_data['dataform_query']
                answer.field_id = field['id']
                answers.append(answer)
        
        # Update the answers
        insert_many(answers)    
        
        # Get Answers again so that we have the pks
        answers = Answer.objects.select_related('submission', 'data_from', 'field').filter(data_form__slug=self.slug, submission=self.submission)
 
            
        # Get All possible choices from form models dict
        choices = self.query_data['choice_query']
        
        # Setup answer list so we can do a bulk update
        answer_objects = []

        #Delete choice relations
        delete_many(answers, table='dataforms_answer_choice')
        
        # We know answers exist now, so update them if needed.            
        for answer in answers:
            answer_obj, choice_relations = self._prepare_answer(answer, choices)
            answer_objects.append(answer_obj)

            if choice_relations:
                for choice_relation in choice_relations:
                    answer.choice.add(choice_relation)
        
        # Update the answers
        update_many(answer_objects, fields=['value'])
            
                    
        # Return a submission so the collection or form can have this.
        return self.submission

    
    def _prepare_answer(self, answer, choices):
        
        field = answer.field
        key = _field_for_form(field, self.slug)
        choice_relations = []

                
        if field.field_type in STATIC_CHOICE_FIELDS:
            answer.value = ','.join(self.cleaned_data[key])
        
        # Because Choices are m2m relations, we need to do this after the save.
        elif field.field_type in CHOICE_FIELDS:
           
            answer_choices = []
           
            # If string, wrap as a list because the for-loop below assumes a list
            if isinstance(self.cleaned_data[key], str) or isinstance(self.cleaned_data[key], unicode):
                self.cleaned_data[key] = [self.cleaned_data[key]]
            
            # Add the selected choices
            for choice_answer in self.cleaned_data[key]:
                cur_choice = filter(lambda x: x.choice.value == choice_answer, choices)
                if cur_choice:
                    cur_choice = cur_choice[0]
                    answer_choices.append(unicode(cur_choice.choice.value))
                    choice_relations.append(cur_choice.choice.pk)
            
            # Save the string representation of the choice answers in to 
            # answer.value
            answer.value = ','.join(answer_choices)
        
        else:
            
            if field.field_type in UPLOAD_FIELDS:
                # We assume that validation of required-ness has already been handled,
                # so only handle the file upload if a file was selected.
                if key in self.files:
                    content = handle_upload(self.files, key, self.submission.id)
                else:
                    content = self.cleaned_data[key]
                    
                    # Don't modify what's in the DB if nothing was submitted,
                    # otherwise, expect an upload path and save this
                    if content:
                        # Remove the MEDIA_URL from this path, to make it easier
                        # to relocate the uploads folder if the media dir changes
                        if settings.MEDIA_URL in content:
                            content = content.replace(settings.MEDIA_URL, "", 1)
                            
            else:
                # Beware the Django pony magic.
                # These conditional checks are required for single checkboxes to work.
                # The unicode() type cast is required to fix an Oracle character
                # code mismatch when saving an integer.

                content = (
                    '1' if self.cleaned_data[key] is True
                    else unicode(self.cleaned_data[key])
                    if self.cleaned_data[key] is not None and self.cleaned_data[key] is not False
                    else ''
                )
        
            answer.value = content
        
        return answer, choice_relations
    
        
    def _remove_extraneous_fields(self, process_full_form):
        """
        Delete extraneous fields that should not be included in form processing.
        This includes hidden bindings fields, note fields, blank file upload
        fields, and fields that were not included in the form POST.
        
        :arg process_full_form: see note on BaseDataForm is_valid()
        """
        
        keys = []
    
        # Get Note and FileInput fields
        fields = Field.objects.filter(dataform__slug=self.meta['slug'], field_type__in=("Note",) + UPLOAD_FIELDS)
        
        # TODO: Remove these when Binding are re-written and Notes are removed.
        # Bindings fields
        keys.append(_field_for_form(name='js_dataform_bindings', form=self.meta['slug']))
        # Note fields
        keys += [_field_for_form(name=field.slug, form=self.meta['slug']) for field in fields if field.field_type == "Note"]
        
        for key in keys:
            if self.fields.has_key(key):
                del self.fields[key]
        
        if not process_full_form:
            # Blank file upload fields
            upload_keys = [_field_for_form(name=field.slug, form=self.meta['slug']) for field in fields if field.field_type in UPLOAD_FIELDS]
            for key in upload_keys:
                if self.data.has_key(key) and self.fields.has_key(key) and not self.data[key].strip():
                    del self.fields[key]
                    
            # Fields that weren't included in the form POST (ignoring upload fields)
            to_delete = []
            for key in self.fields:
                if not self.data.has_key(key) and key not in upload_keys:
                    to_delete.append(key)
            for key in to_delete:
                del self.fields[key]
    
    
    def _generate_bound_fields(self):
        self.bound_fields = SortedDict([(name, BoundField(self, field, name)) for name, field in self.fields.items()])
    
    def _media(self):
        return get_form_media()
    media = property(_media)
    

class BaseCollection(object):
    """
    You shouldn't need to instantiate this object directly, use create_collection.
    
    When you have a collection, here are some tips:: 
    
        # You can see what's next and what came before
        collection.current_section
        collection.next_section
        collection.prev_section
    """
    
    def __init__(self, collection, forms, sections):
        self.collection = collection
        self.submission = None
        self.title = str(collection.title)
        self.description = str(collection.description)
        self.slug = str(collection.slug)
        self.forms = forms
        # Section helpers
        self.sections = sections
        # Set all forms to be viewable initially
        self.set_section()

        
    def __getitem__(self, name):
        """
        Usage::
            # Returns just the specified form
            collection[2]
        """
        
        # The true index to the form the user is asking for is the normal index
        # into self.forms, but excluding forms masked out by __form_existence[] == False
       
        # Only for sequence, so self can be called.
        if isinstance(name, int):
            fake_index = -1
            for i in range(0, len(self.forms) + 1):
                if self.__form_existence[i]:
                    fake_index += 1
                if fake_index == name:
                    return self.forms[i]
        else:
            return getattr(self, name)
        
            
    def __getslice__(self, start, end):
        """
        Make a new collection with the given subset of forms
        """
        
        return BaseCollection(
            title=self.title,
            description=self.description,
            slug=self.slug,
            forms=self.forms[start:end],
            # FIXME: does this need to be limited to the sections of the forms in the slice?
            sections=self.sections 
        )
    
    
    def __len__(self):
        """
        :return: the number of contained forms (that are visible)
        """
        
        return len([truth for truth in self.__form_existence if truth])
       
        
    def save(self):
        """
        Save all contained forms
        """
        
        for form in self:
            if not self.submission:
                self.submission = form.save(collection=self.collection)
            else:
                form.save(collection=self.collection)
        
        
    def is_valid(self, check_required=True, process_full_form=True):
        """
        Validate all contained forms
        """
        
        for form in self:
            if not form.is_valid(check_required=check_required, process_full_form=process_full_form):
                return False
        return True


    def set_section(self, section=None):
        """
        Set the visible section whose forms will be returned
        when using array indexing.
        
        :deprecated: This method is deprecated. Use the section argument to Collection's instead.
        """
        
        if isinstance(section, Section):
            section = section.slug

        if section is None:
            self.__form_existence = [True for form in self.forms]
        else:
            self.__form_existence = [True if form.section == section else False for form in self.forms]
            
        if True not in self.__form_existence:
            raise SectionDoesNotExist(section)
        
        # Set the indexes
        self._section = [row.slug for row in self.sections].index(section) if section else 0
        self._next_section = self._section + 1 if self._section + 1 < len(self.sections) else None
        self._prev_section = self._section - 1 if self._section - 1 >= 0 else None
        
        # Set the objects
        self.section = self.sections[self._section]
        self.next_section = self.sections[self._next_section] if self._next_section is not None else None
        self.prev_section = self.sections[self._prev_section] if self._prev_section is not None else None
    
    def _media(self):
        return get_form_media()
    media = property(_media)


def create_collection(request, collection, submission, readonly=False, section=None):
    """
    Based on a form collection slug, create a list of form objects.
    
    :param request: the current page request object, so we can pull POST and other vars.
    :param collection: a data form collection slug or object
    :param submission: create-on-use submission slug or object; passed in to retrieve
        Answers from an existing Submission, or to be the slug for a new Submission.
    :param readonly: optional readonly; converts form fields to be readonly.
        Usefull for display only logic.
    :param section: optional section; allows a return of only forms on that section. 
    
    :return: a BaseCollection object, populated with the correct data forms and data
    """
    
    # Slightly evil, do type checking to see if collection is a Collection object or string
    if isinstance(collection, str) or isinstance(collection, unicode):
        # Get the queryset for the form collection to pass in our dictionary
        try:
            collection = Collection.objects.get(visible=True, slug=collection)
        except Collection.DoesNotExist:
            raise Collection.DoesNotExist('Collection %s does not exist. Make sure the slug name is correct and the collection is visible.' % collection)
    
    # Get queryset for all the forms that are needed
    try:
        kwargs = {}
        kwargs['collection'] = collection
        kwargs['collection__visible'] = True
        if section:
            kwargs['section__slug'] = section
        forms = CollectionDataForm.objects.select_related('section', 'collection', 'data_form').filter(**kwargs).order_by('order')
    except DataForm.DoesNotExist:
        raise CollectionDataForm.DoesNotExist('Dataforms for collection %s do not exist. Make sure the slug name is correct and the forms are visible.' % collection)
    
    # Get the sections for this collection
    sections = create_sections(collection)
    
    # Initialize a list to contain all the form classes
    form_list = []
    
    # If we are not posting this, then get the answers for the whole collection
    # We do this now instead of in create_form to avoid dup queries
    if not request.POST:
        answers, submission = get_answers(submission=submission, for_form=True)
    else:
        answers = None
    
    # Populate the list
    for form in forms:
        # Hmm...is this evil?
        section = form.section.slug
        temp_form = create_form(request, form=form.data_form, submission=submission, section=section, readonly=readonly, answers=answers)
        form_list.append(temp_form)
    
    # Pass our collection info and our form list to the dictionary
    collection = BaseCollection(
        collection=collection,
        forms=form_list,
        sections=sections,
    )
    
#    t = collection.__dict__
#    assert False
    
    return collection


def create_form(request, form, submission, title=None, description=None, section=None, readonly=False, answers=None, return_class=False):
    """
    Instantiate and return a dynamic form object, optionally already populated from an
    already submitted form.
    
    Usage::
    
        # Get a dynamic form. If a Submission with slug "myForm" exists,
        # this will return a bound form. Otherwise, it will be unbound.
        create_form(request, form="personal-info", submission="myForm")
        
        # Create a bound form to a previous submission object
        create_form(request, slug="personal-info", submission=Submission.objects.get(...))
        
    :param request: the current page request object, so we can pull POST and other vars.
    :param form: a data form slug or object
    :param submission: create-on-use submission slug or object; passed in to retrieve
        Answers from an existing Submission, or to be the slug for a new Submission.
    :param title: optional title; pulled from DB by default
    :param description: optional description; pulled from DB by default
    :param section: optional section; will be added as an attr to the form instance 
    :param readonly: optional readonly; converts form fields to be readonly.
        Usefull for display only logic.
    :param answers: optional answers; answer queryset for the submission
    :param return_class: optional return_class; returns only the form class and decouples database saves
        Usefull for when you want to save the form somewhere else.
    """
            
    # Create our form class and get the querys we used
    FormClass, query_data = _create_form(form=form, title=title, description=description, readonly=readonly)
    
    # TODO: This is not working yet, needs to be completed.
    # Return just the class object if a Form Class is only needed
    # This will de-couple the database integration allowing the developer
    # to save to form like a normal Django form.
    # Note: Bindings do not work if this is coupled with a Formset.
    if return_class:
        return FormClass
    
    # Create the actual form instance, from the dynamic form class
    if request.POST:
        # We assume here that we don't need to overlay the POST data on top of the database
        # data because the initial form, before POST, will contain the database defaults and so
        # the resulting POST data will (in normal cases) originate from database defaults already.
        
        # This creates a bound form object.
        form = FormClass(data=request.POST, files=request.FILES)
    else:
        # We populate the initial data of the form from the database answers. Any questions we
        # don't have answers for in the database will use their initial field defaults.
        
        # This creates an unbound form object.
        
        # Before we populate from submitted data, prepare the answers for insertion into the form
        if answers:
            data = answers 
        elif submission:
            data, submission = get_answers(submission=submission, for_form=True)
        else:
            data = None

        form = FormClass(initial=(data))
        
    # Now that we have an instantiated form object, let's add our custom attributes
    form.submission = submission
    form.section = section
    form.query_data = query_data
    
    return form


def create_sections(collection):
    """
    Create sections of a form collection
    
    :param collection: a data form collection object
    """
    
    # Get the sections from the many-to-many, and then make the elements unique (a set)
    non_unique_sections = (Section.objects.order_by("collectiondataform__order")
                        .filter(collectiondataform__collection=collection).distinct())

    # Force the query to evaluate
    non_unique_sections = list(non_unique_sections)

    # OK, this is evil. We have to manually remove duplicates that exist in the Section queryset.
    # See here for why mixing order_by and distinct returns duplicates.
    #
    # http://docs.djangoproject.com/en/dev/ref/models/querysets/#distinct
    #
    # Also, using list(set(non_unique_sections)) does not work, unfortunately.
    sections = []
    for section in non_unique_sections:
        if section not in sections:
            sections.append(section)
            
    return sections


def _create_form(form, title=None, description=None, readonly=False):
    """
    Creates a form class object.
    
    Usage::
    
        FormClass = _create_form(dataform="myForm")
        form = FormClass(data=request.POST)
    
    :param form: a data form slug or object
    :param title: optional title; pulled from DB by default
    :param description: optional description; pulled from DB by default
    :param readonly: optional readonly; converts form fields to be readonly.
        Usefull for display only logic.
    """
    
    # Make sure the form definition exists before continuing
    # Slightly evil, do type checking to see if form is a DataForm object or string
    # If form object is a slug then get the form object and reassign
    if isinstance(form, str) or isinstance(form, unicode):
        try:
            form = DataForm.objects.get(visible=True, slug=form)
        except DataForm.DoesNotExist:
            raise DataForm.DoesNotExist('DataForm %s does not exist. Make sure the slug name is correct and the form is visible.' % form)
        
    # Otherwise it should be a form model object, if not raise
    elif not isinstance(form, DataForm):
        raise AttributeError('Dataform %s is not a valid data form object.' % form)
    
    meta = {}
    slug = form if isinstance(form, str) or isinstance(form, unicode) else form.slug
    final_fields = SortedDict()
    choices_dict = defaultdict(tuple)
    attrs = {
        'declared_fields' : final_fields,
        'base_fields' : final_fields,
        'meta' : meta,
        'slug' : slug,
    }
    
    # Parse the slug and create a class title
    form_class_title = create_form_class_title(slug)
    
    
    # Set the title and/or the description from the DB (but only if it wasn't given)
    meta['title'] = safe(form.title if not title else title)
    meta['description'] = safe(form.description if not description else description)
    meta['slug'] = form.slug
        
    # Get all the fields
    fields_qs = Field.objects.filter(
        dataformfield__data_form__slug=slug,
        visible=True
    ).order_by('dataformfield__order')
    
    fields = [field for field in fields_qs.values()]

    if not fields:
        raise Field.DoesNotExist('Field for %s do not exist. Make sure the slug name is correct and the fields are visible.' % slug)
    
    # Get all the choices associated to fields
    choices_qs = (
        FieldChoice.objects.select_related('choice', 'field').filter(
            field__dataformfield__data_form__slug=slug,
            field__visible=True
        ).order_by('order')
    )
    
    
    # Get the bindings for use in the Field Loop
    bindings = get_bindings(form=form)
    
    # Add a hidden field used for passing information to the JavaScript bindings function
    fields.append({
        'field_type': 'HiddenInput',
        'slug': 'js_dataform_bindings',
        'initial': safe(force_escape(json.dumps(bindings))),
        'required': False,
    })
    
    
    # Populate our choices dictionary
    for row in choices_qs:
        choices_dict[row.field.pk] += (row.choice.value, safe(row.choice.title)),
        
    # Process the field mappings and import any modules specified by string name
    for key in FIELD_MAPPINGS:
        # Replace the string arguments with the actual modules or classes
        for sub_key in ('class', 'widget'):
            if not FIELD_MAPPINGS[key].has_key(sub_key):
                continue
                
            value = FIELD_MAPPINGS[key][sub_key]
            
            if isinstance(value, str) or isinstance(value, unicode):
                names = value.split(".")
                module_name = ".".join(names[:-1])
                class_name = names[-1]
                module = __import__(module_name, fromlist=[class_name])
                # Replace the string with a class pointer
                FIELD_MAPPINGS[key][sub_key] = getattr(module, class_name)

        # Handle widget arguments
        if not FIELD_MAPPINGS[key].has_key('widget_kwargs'):
            # Initialize all field-mappings that don't have a 'widget_kwargs' key
            FIELD_MAPPINGS[key]['widget_kwargs'] = {}
    
    # ----- Field Loop -----
    # Populate our fields dictionary for this form
    for row in fields:
        form_field_name = _field_for_form(name=row['slug'], form=slug)
        
        field_kwargs = {}
        field_map = FIELD_MAPPINGS[row['field_type']]
        widget_attrs = field_map.get('widget_attrs', {})
        
        if row.has_key('label'):
            field_kwargs['label'] = safe(row['label'])
        if row.has_key('help_text'):
            field_kwargs['help_text'] = safe(row['help_text'])
        if row.has_key('initial'):
            field_kwargs['initial'] = row['initial']
        if row.has_key('required'):
            field_kwargs['required'] = row['required']
            
        additional_field_kwargs = {}
        if row.has_key('arguments') and row['arguments'].strip():
            # Parse any additional field arguments as JSON and include them in field_kwargs
            temp_args = json.loads(str(row['arguments']))
            for arg in temp_args:
                additional_field_kwargs[str(arg)] = temp_args[arg]
        
        # Update the field arguments with the "additional arguments" JSON in the DB
        field_kwargs.update(additional_field_kwargs)
        
        # Get the choices for single and multiple choice fields 
        if row['field_type'] in CHOICE_FIELDS:
            choices = ()
            
            # We add a separator for select boxes
            if row['field_type'] == 'Select':
                choices += ('', '--------'),
            
            # Populate our choices tuple
            choices += choices_dict[row['id']]
            field_kwargs['choices'] = choices
            
            if row['field_type'] in MULTI_CHOICE_FIELDS:
                # Get all of the specified default selected values (as a list, even if one element)
                field_kwargs['initial'] = (
                    field_kwargs['initial'].split(',')
                    if ',' in field_kwargs['initial']
                    else [field_kwargs['initial'], ]
                )
                # Remove whitespace so the user can use spaces
                field_kwargs['initial'] = [element.strip() for element in field_kwargs['initial']]
                
            else:
                field_kwargs['initial'] = ''.join(field_kwargs['initial'])
                
            if readonly:
                widget_attrs['disabled'] = "disabled"
                
        if readonly:
            widget_attrs['readonly'] = 'readonly'
        if readonly and row['field_type'] == "CheckboxInput":
            widget_attrs['disabled'] = "disabled"
          
        # Instantiate the widget that this field will use
        # TODO: Possibly create logic that passes submissionid to file upload widget to handle file
        # paths without enforcing a redirect.
        if field_map.has_key('widget'):
            field_kwargs['widget'] = field_map['widget'](attrs=widget_attrs, **field_map['widget_kwargs'])
        
        # Add this field, including any widgets and additional arguments
        # (initial, label, required, help_text, etc)
        final_field = field_map['class'](**field_kwargs)
        final_field.is_checkbox = (row['field_type'] == 'CheckboxInput')
        final_fields[form_field_name] = final_field

    # Grab the dynamic validation function from validation.py
    if validation:
        validate = getattr(validation, form_class_title, None)
        
        if validate:
            # Pull the "clean_" functions from the validation
            # for this form and inject them into the form object
            for attr_name in dir(validate):
                if attr_name.startswith('clean'):
                    attrs[attr_name] = getattr(validate, attr_name)
    
    # Return a class object of this form with all attributes
    DataFormClass = type(form_class_title, (BaseDataForm,), attrs)

    # Also return the querysets so that they can be re-used
    query_data = {
        'dataform_query' : form,
        'fields_list' : fields,
        'choice_query' : choices_qs,
    }
    
    return DataFormClass, query_data


def get_field_objects(submission):
    """
    Get a list of field objects for a particular submission/collection
    """
    
    # Slightly evil, do type checking to see if submission is a Submission object or string
    if isinstance(submission, str) or isinstance(submission, unicode):
        # Get the queryset for the form collection to pass in our dictionary
        try:
            submission = Submission.objects.get(slug=submission)
        except Submission.DoesNotExist:
            raise Submission.DoesNotExist('Submission %s does not exist. Make sure the slug name is correct.' % submission)
    
    fields = Field.objects.filter(dataform__collection__submission__id=submission.id).order_by('dataform__collectiondataform', 'dataformfield__order')
    
    return fields


def get_answers(submission, for_form=False, form=None, field=None):
    """
    Get the answers for a submission.
    
    This function intentionally does not return the answers in the same
    form as request.POST data will have submitted them (ie, every element
    wrapped as a list). This is because this function is meant to provide
    data that can be instantly consumed by some `FormClass(data=data)`
    instantiation, as done by create_form.
    
    :param submission: A Submission object or slug
    :param for_form: whether or not these answers should be made unique for
        use on a form, ie. if every field slug should be prepended with
        the form's slug. This can be annoying when just wanting to inspect
        answers from a submission, so it is set to False by default, but needs
        to be True when used the keys will be used as form element names.
    :param form: Only get the answer for a specific form. Also accepts a data_form slug.
    :param field: Only get the answer for a specific field. Also accepts a list of field_slugs.
    :return: a dictionary of answers
    """
    
    data = defaultdict(list)
    
    # Slightly evil, do type checking to see if submission is a Submission object or string
    if isinstance(submission, str) or isinstance(submission, unicode):
        try:
            submission = Submission.objects.get(slug=submission)
        except:
            # If no records or error, return empty
            return dict(data), None
   
    elif not isinstance(submission, Submission):
        raise AttributeError('Submission %s is not a valid submission object.' % submission)
        
    submission_id = submission.id
        
    if form:
        if isinstance(form, str) or isinstance(form, unicode):
            form = DataForm.objects.get(slug=form).id
        elif isinstance(form, DataForm):
            form = form.id
    else:
        form = None
    
    # Think in terms of always handling requests for multiple field_slugs, to keep DRY
    field_slugs = field
        
    # Rid ourselves of ORM objects and just use field slug strings
    if field_slugs:
        field_slugs = [(field.slug if isinstance(field, Field) else field) for field in field]
        
        # Transform prepended slugs: personal-information__some-field --> some-field
        field_slugs = [
            (_field_for_db(name=slug) if FIELD_DELIMITER in slug else slug)
            for slug in field_slugs
        ]
    else:
        field_slugs = None
    
    # Populate the query into answers
    answers = Answer.objects.get_answer_data(submission_id, field_slugs, form)
    
    # For every answer, do some magic and get it into our data dictionary
    for answer in answers:
        # TODO: Refactors the answer field name to be globally unique (so
        # that a field can be in multiple forms in the same POST)
        if for_form:
            #answer_key = _field_for_form(name=str(answer['field_slug']), form=answer['dataform_slug'])
            answer_key = _field_for_form(name=answer.field_slug, form=answer.data_form_slug)
        else:
            answer_key = answer.field_slug
        
        # Pass the answer to the Dict Key
        if answer.choice_id:
           
           
            # TODO: Need to check to make sure all Fields are covered.
            # Are there more then string or list?
            if data[answer_key]:
                if not isinstance(data[answer_key], list):
                    data[answer_key] = [data[answer_key]]
                data[answer_key].append(answer.choice_value)
            else:
                if answer.field_type in MULTI_CHOICE_FIELDS:
                    data[answer_key] = [answer.choice_value]
                else:
                    data[answer_key] = answer.choice_value
        else:
            data[answer_key] = answer.value

    # Return the answers and the submission back
    return dict(data), submission


def get_form_media():
    return forms.Media(**FORM_MEDIA)


def get_bindings(form):
    """
    Get the bindings for specific form
    
    :return: list of dictionaries, where each dictionary is a single binding.
    """
    
    if isinstance(form, str) or isinstance(form, unicode):
        form = DataForm.objects.get(slug=form)
            
    bindings = list(Binding.objects.filter(data_form=form).values(
        'id', 'action', 'field', 'field__slug', 'value', 'operator',
        'data_form', 'data_form__slug', 'field_choice', 'field_choice__field__slug',
        'field_choice__choice__value', 'true_field', 'true_choice',
        'false_field', 'false_choice', 'function', 'additional_rules'))
    bindings_list = []

    for binding in bindings:
        
        #if binding['field__slug']:
        binding['selector'] = _field_for_form(name=binding['field__slug'], form=form.slug)
#        else:
#            binding['selector'] = _field_for_form(
#                name='%s___%s' % (binding['field_choice__field__slug'], binding['field_choice__choice__value']),
#                form=form.slug)
        
        for key, value in binding.iteritems():
            if key in ['true_field', 'true_choice', 'false_field','false_choice', 'additional_rules']:
                if value:
                    binding[key] = binding[key].split(',')
                    
                    # Additional split on choice field and its value
                    if key != 'additional_rules':
                        for index, value in enumerate(binding[key]):
                            binding[key][index] = binding[key][index].split('___')
                    
                    
            if not value:
                binding[key] = None
                
        bindings_list.append(binding)

    return bindings_list
    
    
def create_form_class_title(slug):
    """
    Transform "my-form-name" into "MyFormName"
    This is important because we need each form class to have a unique name.
    
    :param slug: the form slug from the DB
    """
    
    return ''.join([word.capitalize() for word in str(slug).split('-')] + ['Form'])


def get_db_field_names(form):

    return [_field_for_db(key) for key in form.fields]


def filter_qs(qs, id):
    return True if qs.id == id else False


def _field_for_form(name, form):
    """
    Make a form field globally unique by prepending the form name
    field-name --> form-name--field-name
    """
    return "%s%s%s" % (form, FIELD_DELIMITER, name)


def _field_for_db(name, packed_return=False):
    """
    Take a form from POST data and get it back to its DB-capable field and form name
    "id_form-name--field-name" --> "field-name"
    "id_form-name--field-name" --> "form-name", "field-name"
    
    :arg packed_return: whether or not to return a tuple of
        (form_name, field_name), or just the field_name
    """
    
    names = name.split(FIELD_DELIMITER)
    
    if packed_return:
        return (names[0][len("id_") if "id_" in names[0] else 0:], names[1])
    else:
        return names[1]
    
    
# Custom dataform exception classes
class RequiredArgument(Exception):
    pass


class SectionDoesNotExist(Exception):
    pass
